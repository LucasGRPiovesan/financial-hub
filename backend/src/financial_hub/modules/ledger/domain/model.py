"""Ledger primitives: accounts, journal entries, postings and balances.

Normative sources: FINANCIAL_MODEL sections 1-2 and ADR-0003. Pure domain code: no I/O and no
persistence imports. The database enforces the same invariants again (constraint triggers and the
``ledger_apply`` function), so a bug on either side is caught by the other.
"""

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Self, cast
from uuid import UUID

from financial_hub.shared.errors import Conflict, FieldError, ValidationFailed
from financial_hub.shared.money import Currency, Money

MAX_CODE_LENGTH: Final = 200
MAX_IDEMPOTENCY_KEY_LENGTH: Final = 255
MAX_RECIPE_LENGTH: Final = 64
PAYLOAD_HASH_VERSION: Final = 1

type JsonValue = str | int | bool | Sequence[JsonValue] | Mapping[str, JsonValue] | None


class AccountType(StrEnum):
    ASSET = "asset"
    LIABILITY = "liability"
    INCOME = "income"
    EXPENSE = "expense"
    EQUITY = "equity"

    @property
    def normal_balance(self) -> NormalBalance:
        """Debit for asset/expense accounts, credit otherwise (FINANCIAL_MODEL s2)."""
        if self in (AccountType.ASSET, AccountType.EXPENSE):
            return NormalBalance.DEBIT
        return NormalBalance.CREDIT


class NormalBalance(StrEnum):
    DEBIT = "debit"
    CREDIT = "credit"


class Direction(StrEnum):
    DEBIT = "debit"
    CREDIT = "credit"

    def opposite(self) -> Direction:
        return Direction.CREDIT if self is Direction.DEBIT else Direction.DEBIT


class EntryStatus(StrEnum):
    """``pending -> posted | archived`` (archived = voided pending entry). Nothing else."""

    PENDING = "pending"
    POSTED = "posted"
    ARCHIVED = "archived"


# --- balances -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BalanceDelta:
    """Signed change of the four balance columns of one account."""

    posted_dr: int = 0
    posted_cr: int = 0
    pending_dr: int = 0
    pending_cr: int = 0

    def __add__(self, other: BalanceDelta) -> BalanceDelta:
        return BalanceDelta(
            self.posted_dr + other.posted_dr,
            self.posted_cr + other.posted_cr,
            self.pending_dr + other.pending_dr,
            self.pending_cr + other.pending_cr,
        )


@dataclass(frozen=True, slots=True)
class Balance:
    """Materialized balance of one account (FINANCIAL_MODEL s2).

    Credit-normal: ``posted = posted_cr - posted_dr``, ``available = posted - pending_dr``.
    Debit-normal: ``posted = posted_dr - posted_cr``, ``available = posted - pending_cr``.
    Pending inflows never increase ``available``.
    """

    currency: Currency
    normal_balance: NormalBalance
    posted_dr: int = 0
    posted_cr: int = 0
    pending_dr: int = 0
    pending_cr: int = 0

    def __post_init__(self) -> None:
        for name in ("posted_dr", "posted_cr", "pending_dr", "pending_cr"):
            value: object = getattr(self, name)
            if type(value) is not int:
                raise TypeError(f"{name} must be int, got {type(value).__name__}")

    @property
    def posted(self) -> Money:
        if self.normal_balance is NormalBalance.CREDIT:
            return Money(self.posted_cr - self.posted_dr, self.currency)
        return Money(self.posted_dr - self.posted_cr, self.currency)

    @property
    def available(self) -> Money:
        if self.normal_balance is NormalBalance.CREDIT:
            return Money(self.posted.minor_units - self.pending_dr, self.currency)
        return Money(self.posted.minor_units - self.pending_cr, self.currency)

    def apply(self, delta: BalanceDelta) -> Balance:
        return Balance(
            self.currency,
            self.normal_balance,
            self.posted_dr + delta.posted_dr,
            self.posted_cr + delta.posted_cr,
            self.pending_dr + delta.pending_dr,
            self.pending_cr + delta.pending_cr,
        )


# --- accounts -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AccountSpec:
    """Template of an account inside a project (FINANCIAL_MODEL s3); provisioned lazily.

    The account is identified by ``(project, code, currency)``; the currency comes from the
    posting amount, so one spec serves every currency.
    """

    code: str
    type: AccountType
    no_overdraft: bool = False
    customer_id: UUID | None = None
    payee_id: UUID | None = None
    provider_account_id: UUID | None = None

    def __post_init__(self) -> None:
        code = self.code
        if (
            not isinstance(code, str)  # pyright: ignore[reportUnnecessaryIsInstance]
            or not 0 < len(code) <= MAX_CODE_LENGTH
            or not code.isascii()
            or not code.isprintable()
            or any(char.isspace() for char in code)
        ):
            raise ValidationFailed(
                f"account code must be 1-{MAX_CODE_LENGTH} printable ASCII characters "
                f"without whitespace, got {code!r}"
            )
        if not isinstance(self.type, AccountType):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError(f"type must be AccountType, got {type(self.type).__name__}")
        if type(self.no_overdraft) is not bool:
            raise TypeError("no_overdraft must be bool")

    @property
    def normal_balance(self) -> NormalBalance:
        return self.type.normal_balance


@dataclass(frozen=True, slots=True)
class Account:
    id: UUID
    project_id: UUID
    spec: AccountSpec
    currency: Currency
    balance: Balance
    version: int = 0

    def __post_init__(self) -> None:
        if self.balance.currency != self.currency:
            raise ValueError("balance currency must equal the account currency")
        if self.balance.normal_balance is not self.spec.normal_balance:
            raise ValueError("balance normal side must follow the account type")

    @property
    def code(self) -> str:
        return self.spec.code

    @property
    def type(self) -> AccountType:
        return self.spec.type

    @property
    def normal_balance(self) -> NormalBalance:
        return self.spec.normal_balance

    @property
    def no_overdraft(self) -> bool:
        return self.spec.no_overdraft


# --- entries --------------------------------------------------------------------------------


def _check_legs(legs: Sequence[tuple[Direction, Money]]) -> None:
    """>= 2 legs, every amount > 0, sum(debits) == sum(credits) per currency."""
    if len(legs) < 2:
        raise ValidationFailed(f"a journal entry needs at least 2 postings, got {len(legs)}")
    totals: dict[Currency, list[int]] = {}
    for index, (direction, amount) in enumerate(legs):
        if not amount.is_positive():
            raise ValidationFailed(
                "posting amounts must be > 0",
                errors=[FieldError(f"/postings/{index}/amount", "must be > 0")],
            )
        sides = totals.setdefault(amount.currency, [0, 0])
        sides[0 if direction is Direction.DEBIT else 1] += amount.minor_units
    unbalanced = sorted(c.code for c, (dr, cr) in totals.items() if dr != cr)
    if unbalanced:
        raise ValidationFailed(f"entry is not balanced in {', '.join(unbalanced)}")


def _freeze_json(value: object, path: str) -> JsonValue:
    """Validate metadata as JSON without floats (no float in money paths) and freeze it."""
    if value is None or isinstance(value, (str, bool, int)):
        return value  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, Mapping):
        frozen: dict[str, JsonValue] = {}
        for key, item in cast("Mapping[object, object]", value).items():
            if not isinstance(key, str):
                raise ValidationFailed(f"metadata keys must be strings at {path}")
            frozen[key] = _freeze_json(item, f"{path}/{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        items = cast("Sequence[object]", value)
        return tuple(_freeze_json(item, f"{path}/{i}") for i, item in enumerate(items))
    raise ValidationFailed(
        f"metadata value at {path} must be JSON (no float), got {type(value).__name__}"
    )


def _thaw_json(value: JsonValue) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_json(item) for item in value]
    return value


def metadata_to_json(metadata: Mapping[str, JsonValue]) -> dict[str, object]:
    """Plain ``dict`` copy of frozen metadata, for serialization."""
    return {key: _thaw_json(value) for key, value in metadata.items()}


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise TypeError(f"{name} must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationFailed(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _check_text(value: object, name: str, max_length: int) -> str:
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= max_length
        or value != value.strip()
        or not value.isprintable()
    ):
        raise ValidationFailed(f"{name} must be 1-{max_length} printable characters, untrimmed")
    return value


@dataclass(frozen=True, slots=True)
class PostingDraft:
    """One leg to be written: ``amount`` may be zero here (zero legs are dropped, never stored)."""

    account: AccountSpec
    direction: Direction
    amount: Money

    def __post_init__(self) -> None:
        if not isinstance(self.direction, Direction):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError("direction must be Direction")
        if not isinstance(self.amount, Money):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError("amount must be Money")
        if self.amount.is_negative():
            raise ValidationFailed(f"posting amount must be >= 0, got {self.amount}")

    @property
    def key(self) -> tuple[str, str]:
        """Account identity inside the project: ``(code, currency)``."""
        return (self.account.code, self.amount.currency.code)


@dataclass(frozen=True, slots=True)
class EntryDraft:
    """A validated entry not yet written. Build it with :meth:`create`.

    Invariants (checked on construction): >= 2 postings, every amount > 0, balanced per currency,
    one project (the draft's), status ``pending`` (two-phase) or ``posted`` (direct post).
    """

    project_id: UUID
    idempotency_key: str
    recipe: str
    postings: tuple[PostingDraft, ...]
    effective_at: datetime
    status: EntryStatus = EntryStatus.POSTED
    reverses_entry_id: UUID | None = None
    correlation_id: UUID | None = None
    causation_id: UUID | None = None
    metadata: Mapping[str, JsonValue] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if not isinstance(self.project_id, UUID):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError("project_id must be UUID")
        _check_text(self.idempotency_key, "idempotency_key", MAX_IDEMPOTENCY_KEY_LENGTH)
        _check_text(self.recipe, "recipe", MAX_RECIPE_LENGTH)
        if self.status not in (EntryStatus.PENDING, EntryStatus.POSTED):
            raise ValidationFailed("a new entry is either pending or posted")
        object.__setattr__(self, "postings", tuple(self.postings))
        _check_legs([(p.direction, p.amount) for p in self.postings])
        object.__setattr__(self, "effective_at", _utc(self.effective_at, "effective_at"))
        frozen = _freeze_json(self.metadata, "")
        if not isinstance(frozen, Mapping):
            raise ValidationFailed("metadata must be a JSON object")
        object.__setattr__(self, "metadata", frozen)

    @classmethod
    def create(
        cls,
        *,
        project_id: UUID,
        idempotency_key: str,
        recipe: str,
        postings: Iterable[PostingDraft],
        effective_at: datetime,
        pending: bool = False,
        reverses_entry_id: UUID | None = None,
        correlation_id: UUID | None = None,
        causation_id: UUID | None = None,
        metadata: Mapping[str, JsonValue] | None = None,
    ) -> Self | None:
        """Entry factory (FINANCIAL_MODEL s1: zero legs are dropped, an empty entry is not created).

        The balance check runs over every leg first (zero legs do not change sums), so a single
        non-zero leg is reported as unbalanced instead of silently disappearing. Returns ``None``
        when every leg is zero.
        """
        legs = tuple(postings)
        totals: dict[Currency, int] = {}
        for leg in legs:
            sign = 1 if leg.direction is Direction.DEBIT else -1
            totals[leg.amount.currency] = (
                totals.get(leg.amount.currency, 0) + sign * leg.amount.minor_units
            )
        unbalanced = sorted(c.code for c, net in totals.items() if net != 0)
        if unbalanced:
            raise ValidationFailed(f"entry is not balanced in {', '.join(unbalanced)}")
        kept = tuple(leg for leg in legs if not leg.amount.is_zero())
        if not kept:
            return None
        return cls(
            project_id=project_id,
            idempotency_key=idempotency_key,
            recipe=recipe,
            postings=kept,
            effective_at=effective_at,
            status=EntryStatus.PENDING if pending else EntryStatus.POSTED,
            reverses_entry_id=reverses_entry_id,
            correlation_id=correlation_id,
            causation_id=causation_id,
            metadata=MappingProxyType(dict(metadata or {})),
        )

    @property
    def payload_hash(self) -> str:
        """Fingerprint of the financial content, compared on idempotent replays (ADR-0003 s7).

        Covers project, recipe, initial status, effective time, reversed entry and the multiset
        of ``(code, currency, direction, amount)``. Delivery data (correlation/causation ids,
        metadata) is excluded: a redelivered fact from another event must replay, not conflict.
        """
        legs = sorted(
            (p.account.code, p.amount.currency.code, p.direction.value, p.amount.minor_units)
            for p in self.postings
        )
        payload = {
            "v": PAYLOAD_HASH_VERSION,
            "project_id": str(self.project_id),
            "recipe": self.recipe,
            "status": self.status.value,
            "effective_at": self.effective_at.isoformat(),
            "reverses_entry_id": str(self.reverses_entry_id) if self.reverses_entry_id else None,
            "postings": legs,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Posting:
    """A stored leg. Immutable; ``amount`` > 0."""

    id: UUID
    account_id: UUID
    direction: Direction
    amount: Money


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """A stored entry with its postings (FINANCIAL_MODEL s2)."""

    id: UUID
    project_id: UUID
    status: EntryStatus
    idempotency_key: str
    payload_hash: str
    recipe: str
    effective_at: datetime
    postings: tuple[Posting, ...]
    reverses_entry_id: UUID | None = None
    correlation_id: UUID | None = None
    causation_id: UUID | None = None
    metadata: Mapping[str, JsonValue] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        _check_legs([(p.direction, p.amount) for p in self.postings])

    def transition_to(self, target: EntryStatus) -> bool:
        """Validate ``self.status -> target``. ``False`` if already there (idempotent no-op)."""
        if self.status is target:
            return False
        if self.status is EntryStatus.PENDING and target in (
            EntryStatus.POSTED,
            EntryStatus.ARCHIVED,
        ):
            return True
        raise Conflict(f"journal entry cannot go from {self.status} to {target}")


def transition_factors(source: EntryStatus | None, target: EntryStatus) -> tuple[int, int]:
    """``(pending_factor, posted_factor)`` applied to the entry's postings for a transition.

    Mirrors the SQL function ``ledger_apply``: ``None`` means not yet applied (new entry).
    """
    match source, target:
        case None, EntryStatus.PENDING:
            return 1, 0
        case None, EntryStatus.POSTED:
            return 0, 1
        case EntryStatus.PENDING, EntryStatus.POSTED:
            return -1, 1
        case EntryStatus.PENDING, EntryStatus.ARCHIVED:
            return -1, 0
        case _ if source is target:
            return 0, 0
        case _:
            raise Conflict(f"journal entry cannot go from {source} to {target}")


def balance_deltas(
    legs: Iterable[tuple[UUID, Direction, Money]], source: EntryStatus | None, target: EntryStatus
) -> dict[UUID, BalanceDelta]:
    """Per-account delta of a status transition for legs ``(account_id, direction, amount)``."""
    pending_factor, posted_factor = transition_factors(source, target)
    deltas: dict[UUID, BalanceDelta] = {}
    for account_id, direction, amount in legs:
        units = amount.minor_units
        if direction is Direction.DEBIT:
            delta = BalanceDelta(posted_dr=posted_factor * units, pending_dr=pending_factor * units)
        else:
            delta = BalanceDelta(posted_cr=posted_factor * units, pending_cr=pending_factor * units)
        deltas[account_id] = deltas.get(account_id, BalanceDelta()) + delta
    return deltas


@dataclass(frozen=True, slots=True)
class TrialBalance:
    """Sums of the balance columns of every account of one project and currency."""

    currency: Currency
    posted_dr: int
    posted_cr: int
    pending_dr: int
    pending_cr: int

    @property
    def posted_net(self) -> int:
        return self.posted_dr - self.posted_cr

    @property
    def pending_net(self) -> int:
        return self.pending_dr - self.pending_cr

    @property
    def is_balanced(self) -> bool:
        return self.posted_net == 0 and self.pending_net == 0
