"""Ledger read side (PRO-36): accounts, entries, as-of balances and the trial balance.

Read-only: nothing here writes or locks, and :class:`LedgerService` is untouched. Every method
takes the project from the caller's ``ProjectScope`` (an id of another project is simply
``NotFound``) and returns plain views with TypeID-prefixed ids and integer minor units.

As-of semantics (documented in the API): a balance or trial balance "as of T" is recomputed from
the postings of entries that are **posted** now and whose ``effective_at <= T``. Pending entries
(two-phase holds) and archived entries never count; ``pending`` is only reported for the current
balance. The status history is not stored, so "posted now" is the filter, not "posted by T".
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any
from uuid import UUID

from financial_hub.modules.ledger.domain import (
    AccountType,
    Balance,
    EntryStatus,
    NormalBalance,
    metadata_to_json,
)
from financial_hub.modules.ledger.infrastructure import queries
from financial_hub.platform.db import UnitOfWork
from financial_hub.shared.errors import FieldError, NotFound, ValidationFailed
from financial_hub.shared.ids import IdPrefix, InvalidIdError, format_id, parse_id
from financial_hub.shared.money import Currency
from financial_hub.shared.pagination import Cursor, Page, PageParams, build_page

_OWNER_COLUMNS: Mapping[IdPrefix, queries.OwnerColumn] = {
    IdPrefix.CUSTOMER: "customer_id",
    IdPrefix.PAYEE: "payee_id",
    IdPrefix.PROVIDER_ACCOUNT: "provider_account_id",
}


@dataclass(frozen=True, slots=True)
class MoneyView:
    amount_minor: int
    currency: str


@dataclass(frozen=True, slots=True)
class BalanceView:
    """``posted``/``available`` as the model defines them (FINANCIAL_MODEL s2).

    ``pending`` is the net of open two-phase holds on the account's normal side
    (``pending_cr - pending_dr`` for credit-normal accounts, ``pending_dr - pending_cr`` for
    debit-normal ones).
    """

    posted: MoneyView
    pending: MoneyView
    available: MoneyView


@dataclass(frozen=True, slots=True)
class AccountView:
    id: str
    code: str
    type: str
    normal_balance: str
    currency: str
    no_overdraft: bool
    customer_id: str | None
    payee_id: str | None
    provider_account_id: str | None
    balance: BalanceView
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AccountBalanceAsOfView:
    account_id: str
    currency: str
    as_of: datetime
    posted: MoneyView
    debits: MoneyView
    credits: MoneyView


@dataclass(frozen=True, slots=True)
class PostingView:
    id: str
    account_id: str
    account_code: str
    direction: str
    amount: MoneyView


@dataclass(frozen=True, slots=True)
class EntryView:
    id: str
    status: str
    recipe: str
    idempotency_key: str
    effective_at: datetime
    created_at: datetime
    reverses_entry_id: str | None
    reversed_by_entry_id: str | None
    correlation_id: str | None
    causation_id: str | None
    metadata: dict[str, object]
    postings: tuple[PostingView, ...] | None = None


@dataclass(frozen=True, slots=True)
class TrialBalanceLineView:
    currency: str
    type: str
    code: str
    debits: int
    credits: int


@dataclass(frozen=True, slots=True)
class TrialBalanceCurrencyView:
    currency: str
    debits: int
    credits: int
    balanced: bool


@dataclass(frozen=True, slots=True)
class TrialBalanceView:
    as_of: datetime | None
    balanced: bool
    totals: tuple[TrialBalanceCurrencyView, ...]
    lines: tuple[TrialBalanceLineView, ...]


def _invalid(pointer: str, detail: str) -> ValidationFailed:
    return ValidationFailed(
        f"invalid query parameter {pointer.lstrip('/')}",
        errors=[FieldError(pointer=pointer, detail=detail, code="invalid")],
    )


def _path_id(value: str, prefix: IdPrefix) -> UUID:
    try:
        return parse_id(value, prefix)
    except InvalidIdError:
        raise NotFound(f"{prefix.value}_ resource not found") from None


def _filter_id(value: str, prefix: IdPrefix, name: str) -> UUID:
    try:
        return parse_id(value, prefix)
    except InvalidIdError as error:
        raise _invalid(f"/{name}", str(error)) from None


def _currency(code: str | None) -> Currency | None:
    if code is None:
        return None
    try:
        return Currency.of(code)
    except ValueError as error:
        raise _invalid("/currency", str(error)) from None


def _aware(value: datetime | None, name: str) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise _invalid(f"/{name}", "must be an RFC 3339 timestamp with a UTC offset")
    return value


def _optional_id(prefix: IdPrefix, value: UUID | None) -> str | None:
    return None if value is None else format_id(prefix, value)


def _account_view(row: Mapping[Any, Any]) -> AccountView:
    currency = Currency.of(row["currency"])
    normal = AccountType(row["type"]).normal_balance
    balance = Balance(
        currency, normal, row["posted_dr"], row["posted_cr"], row["pending_dr"], row["pending_cr"]
    )
    pending = (
        row["pending_cr"] - row["pending_dr"]
        if normal is NormalBalance.CREDIT
        else row["pending_dr"] - row["pending_cr"]
    )
    return AccountView(
        id=format_id(IdPrefix.ACCOUNT, row["id"]),
        code=row["code"],
        type=row["type"],
        normal_balance=normal.value,
        currency=currency.code,
        no_overdraft=row["no_overdraft"],
        customer_id=_optional_id(IdPrefix.CUSTOMER, row["customer_id"]),
        payee_id=_optional_id(IdPrefix.PAYEE, row["payee_id"]),
        provider_account_id=_optional_id(IdPrefix.PROVIDER_ACCOUNT, row["provider_account_id"]),
        balance=BalanceView(
            posted=MoneyView(balance.posted.minor_units, currency.code),
            pending=MoneyView(pending, currency.code),
            available=MoneyView(balance.available.minor_units, currency.code),
        ),
        created_at=row["created_at"],
    )


def _entry_view(row: Mapping[Any, Any]) -> EntryView:
    return EntryView(
        id=format_id(IdPrefix.JOURNAL_ENTRY, row["id"]),
        status=row["status"],
        recipe=row["recipe"],
        idempotency_key=row["idempotency_key"],
        effective_at=row["effective_at"],
        created_at=row["created_at"],
        reverses_entry_id=_optional_id(IdPrefix.JOURNAL_ENTRY, row["reverses_entry_id"]),
        reversed_by_entry_id=_optional_id(IdPrefix.JOURNAL_ENTRY, row["reversed_by_entry_id"]),
        # Same convention as the events explorer: causation is a parent event, correlation a
        # plain UUID (it has no entity prefix).
        correlation_id=None if row["correlation_id"] is None else str(row["correlation_id"]),
        causation_id=_optional_id(IdPrefix.EVENT, row["causation_id"]),
        metadata=metadata_to_json(row["metadata"]),
    )


def _cursor_key(row: Mapping[Any, Any]) -> Cursor:
    return Cursor(created_at=row["created_at"], id=row["id"])


class LedgerQueryService:
    """Read-only ledger queries inside the caller's (read) unit of work."""

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def list_accounts(
        self,
        project_id: UUID,
        params: PageParams,
        *,
        code: str | None = None,
        account_type: AccountType | None = None,
        owner_id: str | None = None,
        currency: str | None = None,
    ) -> Page[AccountView]:
        owner = _owner_of(owner_id)
        rows = await queries.list_accounts(
            self._uow.session,
            project_id,
            code=code,
            account_type=None if account_type is None else account_type.value,
            owner=owner,
            currency=None if (cur := _currency(currency)) is None else cur.code,
            cursor=params.decoded_cursor(),
            limit=params.limit,
        )
        return build_page(rows, params.limit, _account_view, _cursor_key)

    async def get_account(self, project_id: UUID, account_id: str) -> AccountView:
        row = await queries.get_account(
            self._uow.session, project_id, _path_id(account_id, IdPrefix.ACCOUNT)
        )
        if row is None:
            raise NotFound("account not found")
        return _account_view(row)

    async def balance_as_of(
        self, project_id: UUID, account_id: str, as_of: datetime
    ) -> AccountBalanceAsOfView:
        moment = _aware(as_of, "as_of")
        if moment is None:
            raise _invalid("/as_of", "required")
        row = await queries.get_account(
            self._uow.session, project_id, _path_id(account_id, IdPrefix.ACCOUNT)
        )
        if row is None:
            raise NotFound("account not found")
        debits, credits = await queries.posted_sums_as_of(
            self._uow.session, project_id, row["id"], moment
        )
        code = row["currency"]
        normal = AccountType(row["type"]).normal_balance
        posted = credits - debits if normal is NormalBalance.CREDIT else debits - credits
        return AccountBalanceAsOfView(
            account_id=format_id(IdPrefix.ACCOUNT, row["id"]),
            currency=code,
            as_of=moment,
            posted=MoneyView(posted, code),
            debits=MoneyView(debits, code),
            credits=MoneyView(credits, code),
        )

    async def list_entries(
        self,
        project_id: UUID,
        params: PageParams,
        *,
        account_id: str | None = None,
        recipe: str | None = None,
        status: EntryStatus | None = None,
        correlation_id: str | None = None,
        effective_from: datetime | None = None,
        effective_to: datetime | None = None,
    ) -> Page[EntryView]:
        try:
            correlation = None if correlation_id is None else UUID(correlation_id)
        except ValueError:
            raise _invalid("/correlation_id", "must be a UUID") from None
        rows = await queries.list_entries(
            self._uow.session,
            project_id,
            account_id=_account_filter(account_id),
            recipe=recipe,
            status=None if status is None else status.value,
            correlation_id=correlation,
            effective_from=_aware(effective_from, "effective_from"),
            effective_to=_aware(effective_to, "effective_to"),
            cursor=params.decoded_cursor(),
            limit=params.limit,
        )
        return build_page(rows, params.limit, _entry_view, _cursor_key)

    async def get_entry(self, project_id: UUID, entry_id: str) -> EntryView:
        entry_uuid = _path_id(entry_id, IdPrefix.JOURNAL_ENTRY)
        row = await queries.get_entry(self._uow.session, project_id, entry_uuid)
        if row is None:
            raise NotFound("journal entry not found")
        postings = await queries.entry_postings(self._uow.session, project_id, entry_uuid)
        return replace(
            _entry_view(row),
            postings=tuple(
                PostingView(
                    id=format_id(IdPrefix.POSTING, p["id"]),
                    account_id=format_id(IdPrefix.ACCOUNT, p["account_id"]),
                    account_code=p["account_code"],
                    direction=p["direction"],
                    amount=MoneyView(p["amount"], p["currency"]),
                )
                for p in postings
            ),
        )

    async def trial_balance(
        self, project_id: UUID, *, currency: str | None = None, as_of: datetime | None = None
    ) -> TrialBalanceView:
        cur = _currency(currency)
        rows = await queries.trial_balance_lines(
            self._uow.session,
            project_id,
            None if cur is None else cur.code,
            _aware(as_of, "as_of"),
        )
        return _trial_balance(as_of, rows)


def _trial_balance(as_of: datetime | None, rows: Sequence[Mapping[Any, Any]]) -> TrialBalanceView:
    sums: dict[str, list[int]] = {}
    for row in rows:
        total = sums.setdefault(row["currency"], [0, 0])
        total[0] += row["debits"]
        total[1] += row["credits"]
    totals = tuple(
        TrialBalanceCurrencyView(code, debits, credits, debits == credits)
        for code, (debits, credits) in sorted(sums.items())
    )
    return TrialBalanceView(
        as_of=as_of,
        balanced=all(t.balanced for t in totals),
        totals=totals,
        lines=tuple(
            TrialBalanceLineView(r["currency"], r["type"], r["code"], r["debits"], r["credits"])
            for r in rows
        ),
    )


def _account_filter(account_id: str | None) -> UUID | None:
    return None if account_id is None else _filter_id(account_id, IdPrefix.ACCOUNT, "account_id")


def _owner_of(owner_id: str | None) -> tuple[queries.OwnerColumn, UUID] | None:
    if owner_id is None:
        return None
    prefix, _, _ = owner_id.rpartition("_")
    try:
        column = _OWNER_COLUMNS[IdPrefix(prefix)]
    except KeyError, ValueError:
        raise _invalid("/owner_id", "expected a cus_, pay_ or pva_ id") from None
    return column, _filter_id(owner_id, IdPrefix(prefix), "owner_id")
