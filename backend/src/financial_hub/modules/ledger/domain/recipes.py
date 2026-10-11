"""Posting recipes R1-R13 (FINANCIAL_MODEL s4) as pure, deterministic functions.

Each recipe input is a typed, validated value (amounts as :class:`Money`, owners as opaque ids).
``build_*`` functions turn an input plus a :class:`RecipeContext` into journal entry drafts that the
application layer (``application.recipes.apply_recipes``) posts through ``LedgerService`` inside the
caller's transaction. No I/O here. Recipes that need stored state receive it as arguments:

- balance-aware recipes (R8 internal and top-up refunds, R9 opened) get the **locked** balances of
  the accounts listed by :func:`balance_accounts`;
- R9 lost gets the amounts frozen by its R9 opened entry;
- reversals (R5 unblocked, R9 won, R10 returned, R11) and the second phase of two-phase recipes
  (R8, R10) operate on stored entries found by their idempotency keys (``*_key`` functions).

Idempotency keys derive from the business fact (ADR-0003 s7), never from the delivery.
Zero legs are dropped and an all-zero entry is not created (FINANCIAL_MODEL s1).
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final
from uuid import UUID

from financial_hub.modules.ledger.domain.accounts import (
    CHARGEBACK_LOSSES,
    CUSTOMER_CREDITS,
    CUSTOMER_RECEIVABLE,
    CUSTOMER_UNAPPLIED_CASH,
    DISCOUNTS_GIVEN,
    OPENING_BALANCE,
    OTHER_INCOME,
    PAYEE_PAYABLE_AVAILABLE,
    PAYEE_PAYABLE_HELD,
    PAYEE_PAYABLE_PENDING,
    PAYEE_RECEIVABLE,
    PLATFORM_REVENUE,
    PROVIDER_BALANCE,
    PROVIDER_BALANCE_HELD,
    PROVIDER_FEES,
    PROVIDER_RECEIVABLE,
    SALES_CLEARING,
    CustomerId,
    PayeeId,
    ProviderAccountId,
)
from financial_hub.modules.ledger.domain.model import (
    AccountSpec,
    Balance,
    Direction,
    EntryDraft,
    JsonValue,
    PostingDraft,
)
from financial_hub.shared.errors import ValidationFailed
from financial_hub.shared.money import Currency, Money, allocate

REASON_CODE: Final = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")


class RecipeCode(StrEnum):
    """Value stored in ``journal_entries.recipe``."""

    R1 = "R1"
    R2 = "R2"
    R2A = "R2a"
    R2B = "R2b"
    R3 = "R3"
    R4 = "R4"
    R5 = "R5"
    R6 = "R6"
    R7 = "R7"
    R8 = "R8"
    R9 = "R9"
    R10 = "R10"
    R11 = "R11"
    R12 = "R12"
    R13 = "R13"


class ReceivedInCashRejected(ValidationFailed):
    """``RECEIVED_IN_CASH`` moves money that never reached the provider: no recipe books it.

    The caller parks the event and raises an alert (FINANCIAL_MODEL s4, "Not supported").
    """


class FeeBearer(StrEnum):
    """R3 split basis ``X`` policy: ``proportional`` -> ``N``, ``platform`` -> ``B``."""

    PROPORTIONAL = "proportional"
    PLATFORM = "platform"


class PayeeBucket(StrEnum):
    """Which payee liability holds the share when a provider split executes (R5)."""

    PENDING = "pending"
    AVAILABLE = "available"


class RefundOutcome(StrEnum):
    """Provider outcome of a refund: ``DONE`` posts the hold, the others void it (R8 step 2)."""

    DONE = "done"
    DENIED = "denied"
    CANCELLED = "cancelled"


# --- shared value types -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecipeContext:
    """Where and when a recipe is booked; shared by every recipe of one unit of work.

    ``effective_at`` is part of the payload hash: a replay of the same fact must pass the same
    value (the fact's time, e.g. the provider event date), never ``now()``.
    """

    project_id: UUID
    effective_at: datetime
    correlation_id: UUID | None = None
    causation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class Approval:
    """Reference to a granted four-eyes approval (maker-checker lives in later tasks).

    Recipes that require one (R10 request, R12, R13) cannot be built without it. Only the
    approval id is stored in the entry metadata.
    """

    approval_id: UUID
    requested_by: UUID
    approved_by: UUID

    def __post_init__(self) -> None:
        for name in ("approval_id", "requested_by", "approved_by"):
            if not isinstance(getattr(self, name), UUID):
                raise TypeError(f"{name} must be UUID")
        if self.requested_by == self.approved_by:
            raise ValidationFailed("four-eyes approval: approver must differ from requester")


@dataclass(frozen=True, slots=True)
class PayeeAmount:
    payee_id: PayeeId
    amount: Money

    def __post_init__(self) -> None:
        _require_money(self.amount, "amount")
        if self.amount.is_negative():
            raise ValidationFailed(f"payee amount must be >= 0, got {self.amount}")


@dataclass(frozen=True, slots=True)
class SplitShare:
    payee_id: PayeeId
    weight: int | Decimal


@dataclass(frozen=True, slots=True)
class SplitAllocation:
    """Parts ``[s_1 ... s_n, s_platform]`` of an amount (platform last, FINANCIAL_MODEL s1)."""

    payees: tuple[PayeeAmount, ...]
    platform: Money

    def __post_init__(self) -> None:
        object.__setattr__(self, "payees", tuple(self.payees))
        _require_money(self.platform, "platform")
        if self.platform.is_negative():
            raise ValidationFailed(f"platform part must be >= 0, got {self.platform}")
        _unique_payees(p.payee_id for p in self.payees)
        _currency(self.platform, *(p.amount for p in self.payees))

    @property
    def currency(self) -> Currency:
        return self.platform.currency

    @property
    def payee_total(self) -> Money:
        return sum((p.amount for p in self.payees), Money.zero(self.currency))

    @property
    def total(self) -> Money:
        return self.payee_total + self.platform

    def weights(self) -> list[int]:
        return [p.amount.minor_units for p in self.payees] + [self.platform.minor_units]

    def reallocate(self, amount: Money) -> SplitAllocation:
        """Allocate ``amount`` over these parts (largest remainder, platform last)."""
        if self.total.is_zero():
            raise ValidationFailed("cannot allocate over parts that sum to zero")
        parts = allocate(amount, self.weights())
        return SplitAllocation(
            tuple(PayeeAmount(p.payee_id, s) for p, s in zip(self.payees, parts, strict=False)),
            parts[-1],
        )


@dataclass(frozen=True, slots=True)
class SplitRule:
    """Split weights per payee plus the platform weight (``allocate(X, weights)``).

    Weights are non-negative ``int`` or ``Decimal`` (percentages such as ``Decimal("12.5")``),
    never float. The platform is always the last participant (rounding tie-break).
    """

    payees: tuple[SplitShare, ...]
    platform_weight: int | Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "payees", tuple(self.payees))
        _unique_payees(s.payee_id for s in self.payees)
        try:
            allocate(Money.zero(_ANY_CURRENCY), self._weights())
        except (TypeError, ValueError) as exc:
            raise ValidationFailed(f"invalid split weights: {exc}") from None

    def _weights(self) -> list[int | Decimal]:
        return [s.weight for s in self.payees] + [self.platform_weight]

    def allocate(self, basis: Money) -> SplitAllocation:
        if basis.is_negative():
            raise ValidationFailed(f"split basis must be >= 0, got {basis}")
        parts = allocate(basis, self._weights())
        return SplitAllocation(
            tuple(PayeeAmount(s.payee_id, p) for s, p in zip(self.payees, parts, strict=False)),
            parts[-1],
        )


@dataclass(frozen=True, slots=True)
class RefundedSplit:
    """One ``refundedSplits`` item of an Asaas refund (provider split)."""

    payee_id: PayeeId
    amount: Money
    done: bool


# --- validation helpers ---------------------------------------------------------------------------

_ANY_CURRENCY: Final = Currency.of("BRL")


def _require_money(value: object, name: str) -> Money:
    if not isinstance(value, Money):
        raise TypeError(f"{name} must be Money, got {type(value).__name__}")
    return value


def _currency(*amounts: Money) -> Currency:
    currencies = {_require_money(a, "amount").currency for a in amounts}
    if len(currencies) != 1:
        codes = ", ".join(sorted(c.code for c in currencies))
        raise ValidationFailed(f"a recipe uses exactly one currency, got {codes}")
    return next(iter(currencies))


def _positive(amount: Money, name: str) -> None:
    if not _require_money(amount, name).is_positive():
        raise ValidationFailed(f"{name} must be > 0, got {amount}")


def _non_negative(amount: Money, name: str) -> None:
    if _require_money(amount, name).is_negative():
        raise ValidationFailed(f"{name} must be >= 0, got {amount}")


def _unique_payees(payees: Iterable[PayeeId]) -> None:
    seen: set[PayeeId] = set()
    for payee in payees:
        if payee in seen:
            raise ValidationFailed(f"payee {payee} appears twice")
        seen.add(payee)


def _reason(reason: str) -> None:
    # A code, not free text: entry metadata must never carry PII.
    if not isinstance(reason, str) or not REASON_CODE.fullmatch(reason):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ValidationFailed(
            "reason must be a code matching [a-z0-9][a-z0-9_.-]{0,63} (no free text)"
        )


def _approval(approval: object) -> None:
    if not isinstance(approval, Approval):
        raise ValidationFailed("this recipe requires an Approval reference (four-eyes)")


# --- idempotency keys -----------------------------------------------------------------------------


def _key(kind: str, fact_id: UUID, *parts: str) -> str:
    return ":".join((kind, str(fact_id), *parts))


def invoice_finalized_key(invoice_id: UUID) -> str:
    return _key("invoice", invoice_id, "finalized")


def invoice_wallet_key(invoice_id: UUID) -> str:
    return _key("invoice", invoice_id, "wallet")


def payment_captured_key(payment_id: UUID) -> str:
    return _key("payment", payment_id, "captured")


def refund_pending_key(refund_id: UUID) -> str:
    return _key("refund", refund_id, "pending")


def dispute_opened_key(dispute_id: UUID) -> str:
    return _key("dispute", dispute_id, "opened")


def payout_pending_key(payout_id: UUID) -> str:
    return _key("payout", payout_id, "pending")


def split_blocked_key(split_id: UUID) -> str:
    return _key("split", split_id, "blocked")


# --- recipe inputs --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InvoiceFinalized:
    """R1. ``sales_amount`` = ``B_sales`` (sales lines only), ``wallet_applied`` = ``u``."""

    invoice_id: UUID
    customer_id: CustomerId
    sales_amount: Money
    wallet_applied: Money

    def __post_init__(self) -> None:
        _currency(self.sales_amount, self.wallet_applied)
        _non_negative(self.wallet_applied, "wallet_applied")
        if self.wallet_applied > self.sales_amount:
            raise ValidationFailed("wallet_applied cannot exceed the sales amount")


@dataclass(frozen=True, slots=True)
class WalletConsumed:
    """R7 (same unit of work as R1): ``amount`` = ``u``, split basis ``X = u``, ``F = 0``."""

    invoice_id: UUID
    customer_id: CustomerId
    amount: Money
    split: SplitRule

    def __post_init__(self) -> None:
        _non_negative(self.amount, "amount")


@dataclass(frozen=True, slots=True)
class PaymentCaptured:
    """R2 (first capture of the intent). ``billed`` = ``B``, ``gross`` = ``G``, ``fee`` = ``F``."""

    payment_id: UUID
    customer_id: CustomerId
    provider_account_id: ProviderAccountId
    billed: Money
    gross: Money
    fee: Money

    def __post_init__(self) -> None:
        _check_capture(self.billed, self.gross, self.fee, "billed")

    @property
    def net(self) -> Money:
        """``N = G - F`` (``netValue``)."""
        return self.gross - self.fee


@dataclass(frozen=True, slots=True)
class FeeAdjusted:
    """R2a. ``booked_fee`` is the fee already in the ledger; ``net_value`` the new ``netValue``."""

    payment_id: UUID
    provider_account_id: ProviderAccountId
    gross: Money
    booked_fee: Money
    net_value: Money

    def __post_init__(self) -> None:
        _currency(self.gross, self.booked_fee, self.net_value)
        _non_negative(self.booked_fee, "booked_fee")
        _non_negative(self.net_value, "net_value")
        if self.net_value > self.gross or self.booked_fee > self.gross:
            raise ValidationFailed("net_value and booked_fee cannot exceed gross")


@dataclass(frozen=True, slots=True)
class UnappliedCashReceived:
    """R2b: a capture with no open obligation (second/late capture)."""

    payment_id: UUID
    customer_id: CustomerId
    provider_account_id: ProviderAccountId
    gross: Money
    fee: Money

    def __post_init__(self) -> None:
        _check_capture(self.gross, self.gross, self.fee, "gross")


@dataclass(frozen=True, slots=True)
class SplitAllocated:
    """R3 (same unit of work as R2; sales lines only)."""

    payment_id: UUID
    billed: Money
    net: Money
    fee_bearer: FeeBearer
    split: SplitRule

    def __post_init__(self) -> None:
        _currency(self.billed, self.net)
        _non_negative(self.billed, "billed")
        _non_negative(self.net, "net")
        if not isinstance(self.fee_bearer, FeeBearer):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError("fee_bearer must be FeeBearer")
        allocation = self.allocation()
        if allocation.payee_total > self.net:
            raise ValidationFailed("payee shares exceed the net value (sum(s_i) > N)")
        if allocation.payee_total > self.billed:
            raise ValidationFailed("payee shares exceed the billed amount (sum(s_i) > B)")

    @property
    def basis(self) -> Money:
        return self.net if self.fee_bearer is FeeBearer.PROPORTIONAL else self.billed

    def allocation(self) -> SplitAllocation:
        """``[s_1 ... s_n, s_platform] = allocate(X, weights)``; persist it for R4/R8/R9."""
        return self.split.allocate(self.basis)


@dataclass(frozen=True, slots=True)
class PaymentSettled:
    """R4. ``internal_shares``: the R3 payee shares (empty for provider split, top-up, no split)."""

    payment_id: UUID
    provider_account_id: ProviderAccountId
    net: Money
    internal_shares: tuple[PayeeAmount, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "internal_shares", tuple(self.internal_shares))
        _non_negative(self.net, "net")
        _currency(self.net, *(s.amount for s in self.internal_shares))
        _unique_payees(s.payee_id for s in self.internal_shares)


@dataclass(frozen=True, slots=True)
class ProviderSplitDone:
    """R5 ``split.done``: the provider moved ``amount`` to the payee's subaccount."""

    split_id: UUID
    payee_id: PayeeId
    provider_account_id: ProviderAccountId
    amount: Money
    bucket: PayeeBucket = PayeeBucket.PENDING

    def __post_init__(self) -> None:
        _positive(self.amount, "amount")


@dataclass(frozen=True, slots=True)
class ProviderSplitBlocked:
    """R5 ``split.blocked``: the provider holds ``amount`` because of a divergence."""

    split_id: UUID
    provider_account_id: ProviderAccountId
    amount: Money

    def __post_init__(self) -> None:
        _positive(self.amount, "amount")


@dataclass(frozen=True, slots=True)
class ProviderSplitUnblocked:
    """R5 unblock: reverses the ``split.blocked`` entry of ``split_id``."""

    split_id: UUID


@dataclass(frozen=True, slots=True)
class TopUpCaptured:
    """R6: capture of a credit purchase (``credits`` = ``A``), instead of R2/R3."""

    payment_id: UUID
    customer_id: CustomerId
    provider_account_id: ProviderAccountId
    credits: Money
    gross: Money
    fee: Money

    def __post_init__(self) -> None:
        _check_capture(self.credits, self.gross, self.fee, "credits")


@dataclass(frozen=True, slots=True, kw_only=True)
class RefundRequest:
    """Common fields of R8 step 1 (pending entry).

    ``already_refunded`` = refunds of the same payment booked **before** this one (pending or
    posted, never this refund itself), so a replay of this fact hashes the same. Cumulative refunds
    are capped at ``gross`` (``G``). ``settled`` selects the source of funds: ``provider_balance``
    when settled, else ``provider_receivable``. The provider fee is non-refundable.
    """

    refund_id: UUID
    payment_id: UUID
    provider_account_id: ProviderAccountId
    amount: Money
    gross: Money
    already_refunded: Money
    settled: bool

    def _check_refund(self) -> None:
        _currency(self.amount, self.gross, self.already_refunded)
        _positive(self.amount, "amount")
        _non_negative(self.already_refunded, "already_refunded")
        if self.already_refunded + self.amount > self.gross:
            raise ValidationFailed("cumulative refunds cannot exceed the gross collected (G)")
        if type(self.settled) is not bool:
            raise TypeError("settled must be bool")

    @property
    def source(self) -> AccountSpec:
        template = PROVIDER_BALANCE if self.settled else PROVIDER_RECEIVABLE
        return template.spec(self.provider_account_id)


@dataclass(frozen=True, slots=True, kw_only=True)
class RefundRequested(RefundRequest):
    """R8, internal split or no split. ``original``: the R3 credited parts (payees ``s_i``,
    platform ``B - sum(s_i)``); with no split, no payees and platform ``B``."""

    original: SplitAllocation

    def __post_init__(self) -> None:
        self._check_refund()
        _currency(self.amount, self.original.platform)
        if self.original.total.is_zero():
            raise ValidationFailed("original parts must sum to more than zero")


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderSplitRefundRequested(RefundRequest):
    """R8, provider split: ``refunded_splits`` as reported by the provider for this refund."""

    refunded_splits: tuple[RefundedSplit, ...] = field(default=())

    def __post_init__(self) -> None:
        object.__setattr__(self, "refunded_splits", tuple(self.refunded_splits))
        self._check_refund()
        for item in self.refunded_splits:
            _currency(self.amount, item.amount)
            _positive(item.amount, "refunded split amount")
            if type(item.done) is not bool:
                raise TypeError("refunded split done must be bool")
        _unique_payees(item.payee_id for item in self.refunded_splits)
        if self.splits_total > self.amount:
            raise ValidationFailed("refunded splits exceed the refund amount")

    @property
    def splits_total(self) -> Money:
        return sum((s.amount for s in self.refunded_splits), Money.zero(self.amount.currency))


@dataclass(frozen=True, slots=True, kw_only=True)
class TopUpRefundRequested(RefundRequest):
    """R8, top-up refund: credits first, shortfall to the customer's receivable."""

    customer_id: CustomerId

    def __post_init__(self) -> None:
        self._check_refund()


@dataclass(frozen=True, slots=True, kw_only=True)
class UnappliedCashRefundRequested(RefundRequest):
    """R8, refund of unapplied cash (R2b)."""

    customer_id: CustomerId

    def __post_init__(self) -> None:
        self._check_refund()


@dataclass(frozen=True, slots=True)
class RefundCompleted:
    """R8 step 2: ``DONE`` posts the pending entry; ``DENIED``/``CANCELLED`` void it."""

    refund_id: UUID
    outcome: RefundOutcome


@dataclass(frozen=True, slots=True)
class DisputeOpened:
    """R9 opened. ``amount`` = ``D``; ``original``: the R3 credited parts of the payment."""

    dispute_id: UUID
    provider_account_id: ProviderAccountId
    amount: Money
    original: SplitAllocation

    def __post_init__(self) -> None:
        _positive(self.amount, "amount")
        _currency(self.amount, self.original.platform)
        if self.original.total.is_zero():
            raise ValidationFailed("original parts must sum to more than zero")

    def shares(self) -> SplitAllocation:
        """``d_i = allocate(D)`` over the original parts."""
        return self.original.reallocate(self.amount)


@dataclass(frozen=True, slots=True)
class DisputeWon:
    """R9 won: reverses the R9 opened entry."""

    dispute_id: UUID


@dataclass(frozen=True, slots=True)
class DisputeLost:
    """R9 lost. Same ``amount``/``original`` as when opened; ``chargeback_fee`` may be zero."""

    dispute_id: UUID
    provider_account_id: ProviderAccountId
    amount: Money
    original: SplitAllocation
    chargeback_fee: Money

    def __post_init__(self) -> None:
        _positive(self.amount, "amount")
        _non_negative(self.chargeback_fee, "chargeback_fee")
        _currency(self.amount, self.chargeback_fee, self.original.platform)
        if self.original.total.is_zero():
            raise ValidationFailed("original parts must sum to more than zero")

    def shares(self) -> SplitAllocation:
        return self.original.reallocate(self.amount)


@dataclass(frozen=True, slots=True)
class PayoutRequested:
    """R10 step 1 (four-eyes approved): pending ``Dr payee_payable_available``,
    ``Cr provider_balance``."""

    payout_id: UUID
    payee_id: PayeeId
    provider_account_id: ProviderAccountId
    amount: Money
    approval: Approval

    def __post_init__(self) -> None:
        _positive(self.amount, "amount")
        _approval(self.approval)


@dataclass(frozen=True, slots=True)
class PayoutPaid:
    """R10 step 2: posts the pending payout and books the transfer fee (may be zero)."""

    payout_id: UUID
    provider_account_id: ProviderAccountId
    transfer_fee: Money

    def __post_init__(self) -> None:
        _non_negative(self.transfer_fee, "transfer_fee")


@dataclass(frozen=True, slots=True)
class PayoutFailed:
    """R10 step 3 (failed, cancelled or rejected by withdrawal validation): voids the hold."""

    payout_id: UUID


@dataclass(frozen=True, slots=True)
class PayoutReturned:
    """R10 step 4: the transfer came back after being paid; reverses the payout entry."""

    payout_id: UUID


@dataclass(frozen=True, slots=True)
class InvoiceVoided:
    """R11: reverses the R1 and R7 entries of the invoice (caller checks nothing was captured)."""

    invoice_id: UUID


@dataclass(frozen=True, slots=True)
class ReconciliationAdjustment:
    """R12 (approved break resolution): ``amount > 0`` debits ``provider_balance`` (the provider
    holds more than booked), ``amount < 0`` credits it; the other side is ``opening_balance``.
    Fee differences use R2a instead."""

    adjustment_id: UUID
    provider_account_id: ProviderAccountId
    amount: Money
    reason: str
    approval: Approval

    def __post_init__(self) -> None:
        if _require_money(self.amount, "amount").is_zero():
            raise ValidationFailed("adjustment amount must not be zero")
        _reason(self.reason)
        _approval(self.approval)


@dataclass(frozen=True, slots=True)
class WriteOff:
    """R13 (approved): ``Dr chargeback_losses / Cr payee_receivable:{p}`` or
    ``customer_receivable:{c}``; exactly one of ``payee_id``/``customer_id``."""

    write_off_id: UUID
    amount: Money
    reason: str
    approval: Approval
    payee_id: PayeeId | None = None
    customer_id: CustomerId | None = None

    def __post_init__(self) -> None:
        _positive(self.amount, "amount")
        _reason(self.reason)
        _approval(self.approval)
        if (self.payee_id is None) == (self.customer_id is None):
            raise ValidationFailed("a write-off targets exactly one payee or one customer")

    @property
    def target(self) -> AccountSpec:
        if self.payee_id is not None:
            return PAYEE_RECEIVABLE.spec(self.payee_id)
        return CUSTOMER_RECEIVABLE.spec(self.customer_id)


@dataclass(frozen=True, slots=True)
class ReceivedInCash:
    """Asaas ``RECEIVED_IN_CASH``: always rejected with :class:`ReceivedInCashRejected`."""

    payment_id: UUID


type RecipeInput = (
    InvoiceFinalized
    | WalletConsumed
    | PaymentCaptured
    | FeeAdjusted
    | UnappliedCashReceived
    | SplitAllocated
    | PaymentSettled
    | ProviderSplitDone
    | ProviderSplitBlocked
    | ProviderSplitUnblocked
    | TopUpCaptured
    | RefundRequested
    | ProviderSplitRefundRequested
    | TopUpRefundRequested
    | UnappliedCashRefundRequested
    | RefundCompleted
    | DisputeOpened
    | DisputeWon
    | DisputeLost
    | PayoutRequested
    | PayoutPaid
    | PayoutFailed
    | PayoutReturned
    | InvoiceVoided
    | ReconciliationAdjustment
    | WriteOff
    | ReceivedInCash
)


def _check_capture(credited: Money, gross: Money, fee: Money, credited_name: str) -> None:
    _currency(credited, gross, fee)
    _non_negative(credited, credited_name)
    _positive(gross, "gross")
    _non_negative(fee, "fee")
    if fee > gross:
        raise ValidationFailed("fee cannot exceed gross")


# --- builders -------------------------------------------------------------------------------------


def _dr(account: AccountSpec, amount: Money) -> PostingDraft:
    return PostingDraft(account, Direction.DEBIT, amount)


def _cr(account: AccountSpec, amount: Money) -> PostingDraft:
    return PostingDraft(account, Direction.CREDIT, amount)


def _signed(account: AccountSpec, amount: Money) -> PostingDraft:
    """Debit for a positive amount, credit of its magnitude for a negative one."""
    return _dr(account, amount) if not amount.is_negative() else _cr(account, -amount)


def _entry(
    ctx: RecipeContext,
    key: str,
    recipe: RecipeCode,
    legs: Iterable[PostingDraft],
    metadata: Mapping[str, JsonValue],
    *,
    pending: bool = False,
) -> tuple[EntryDraft, ...]:
    draft = EntryDraft.create(
        project_id=ctx.project_id,
        idempotency_key=key,
        recipe=recipe.value,
        postings=legs,
        effective_at=ctx.effective_at,
        pending=pending,
        correlation_id=ctx.correlation_id,
        causation_id=ctx.causation_id,
        metadata=metadata,
    )
    return () if draft is None else (draft,)


def _capture_legs(
    provider: ProviderAccountId,
    credit_account: AccountSpec,
    credited: Money,
    gross: Money,
    fee: Money,
) -> list[PostingDraft]:
    """R2 legs (also R2b and R6): gross in, credited out, difference to other income/discount."""
    zero = Money.zero(gross.currency)
    receivable = PROVIDER_RECEIVABLE.spec(provider)
    return [
        _dr(receivable, gross),
        _cr(credit_account, credited),
        _cr(OTHER_INCOME.spec(), max(gross - credited, zero)),
        _dr(DISCOUNTS_GIVEN.spec(), max(credited - gross, zero)),
        _dr(PROVIDER_FEES.spec(), fee),
        _cr(receivable, fee),
    ]


def _drain(
    amount: Money, buckets: Sequence[tuple[AccountSpec, Balance | None]]
) -> tuple[list[tuple[AccountSpec, Money]], Money]:
    """Take ``amount`` from ``buckets`` in order, each up to its ``available`` (never below 0)."""
    zero = Money.zero(amount.currency)
    rest = amount
    taken: list[tuple[AccountSpec, Money]] = []
    for account, balance in buckets:
        available = zero if balance is None else max(balance.available, zero)
        take = min(rest, available)
        taken.append((account, take))
        rest -= take
    return taken, rest


def build_r1(ctx: RecipeContext, r: InvoiceFinalized) -> tuple[EntryDraft, ...]:
    due = r.sales_amount - r.wallet_applied
    return _entry(
        ctx,
        invoice_finalized_key(r.invoice_id),
        RecipeCode.R1,
        [_dr(CUSTOMER_RECEIVABLE.spec(r.customer_id), due), _cr(SALES_CLEARING.spec(), due)],
        {"invoice_id": str(r.invoice_id)},
    )


def build_r7(ctx: RecipeContext, r: WalletConsumed) -> tuple[EntryDraft, ...]:
    shares = r.split.allocate(r.amount)
    legs = [
        _dr(CUSTOMER_CREDITS.spec(r.customer_id), r.amount),
        _cr(SALES_CLEARING.spec(), r.amount),
        _dr(SALES_CLEARING.spec(), r.amount),
        *(_cr(PAYEE_PAYABLE_AVAILABLE.spec(s.payee_id), s.amount) for s in shares.payees),
        _cr(PLATFORM_REVENUE.spec(), shares.platform),
    ]
    return _entry(
        ctx,
        invoice_wallet_key(r.invoice_id),
        RecipeCode.R7,
        legs,
        {"invoice_id": str(r.invoice_id)},
    )


def build_r2(ctx: RecipeContext, r: PaymentCaptured) -> tuple[EntryDraft, ...]:
    legs = _capture_legs(
        r.provider_account_id, CUSTOMER_RECEIVABLE.spec(r.customer_id), r.billed, r.gross, r.fee
    )
    return _entry(
        ctx,
        payment_captured_key(r.payment_id),
        RecipeCode.R2,
        legs,
        {"payment_id": str(r.payment_id)},
    )


def build_r2a(ctx: RecipeContext, r: FeeAdjusted) -> tuple[EntryDraft, ...]:
    delta = (r.gross - r.net_value) - r.booked_fee
    receivable = PROVIDER_RECEIVABLE.spec(r.provider_account_id)
    fees = PROVIDER_FEES.spec()
    return _entry(
        ctx,
        _key("payment", r.payment_id, "fee", str(r.net_value.minor_units)),
        RecipeCode.R2A,
        [_signed(fees, delta), _signed(receivable, -delta)],
        {"payment_id": str(r.payment_id)},
    )


def build_r2b(ctx: RecipeContext, r: UnappliedCashReceived) -> tuple[EntryDraft, ...]:
    legs = _capture_legs(
        r.provider_account_id,
        CUSTOMER_UNAPPLIED_CASH.spec(r.customer_id),
        r.gross,
        r.gross,
        r.fee,
    )
    return _entry(
        ctx,
        _key("payment", r.payment_id, "unapplied"),
        RecipeCode.R2B,
        legs,
        {"payment_id": str(r.payment_id)},
    )


def build_r3(ctx: RecipeContext, r: SplitAllocated) -> tuple[EntryDraft, ...]:
    shares = r.allocation()
    legs = [
        _dr(SALES_CLEARING.spec(), r.billed),
        *(_cr(PAYEE_PAYABLE_PENDING.spec(s.payee_id), s.amount) for s in shares.payees),
        _cr(PLATFORM_REVENUE.spec(), r.billed - shares.payee_total),
    ]
    return _entry(
        ctx,
        _key("payment", r.payment_id, "split"),
        RecipeCode.R3,
        legs,
        {"payment_id": str(r.payment_id), "fee_bearer": r.fee_bearer.value},
    )


def build_r4(ctx: RecipeContext, r: PaymentSettled) -> tuple[EntryDraft, ...]:
    legs = [
        _dr(PROVIDER_BALANCE.spec(r.provider_account_id), r.net),
        _cr(PROVIDER_RECEIVABLE.spec(r.provider_account_id), r.net),
    ]
    for share in r.internal_shares:
        legs.append(_dr(PAYEE_PAYABLE_PENDING.spec(share.payee_id), share.amount))
        legs.append(_cr(PAYEE_PAYABLE_AVAILABLE.spec(share.payee_id), share.amount))
    return _entry(
        ctx,
        _key("payment", r.payment_id, "settled"),
        RecipeCode.R4,
        legs,
        {"payment_id": str(r.payment_id)},
    )


def build_r5_done(ctx: RecipeContext, r: ProviderSplitDone) -> tuple[EntryDraft, ...]:
    bucket = PAYEE_PAYABLE_PENDING if r.bucket is PayeeBucket.PENDING else PAYEE_PAYABLE_AVAILABLE
    return _entry(
        ctx,
        _key("split", r.split_id, "done"),
        RecipeCode.R5,
        [
            _dr(bucket.spec(r.payee_id), r.amount),
            _cr(PROVIDER_BALANCE.spec(r.provider_account_id), r.amount),
        ],
        {"split_id": str(r.split_id)},
    )


def build_r5_blocked(ctx: RecipeContext, r: ProviderSplitBlocked) -> tuple[EntryDraft, ...]:
    return _entry(
        ctx,
        split_blocked_key(r.split_id),
        RecipeCode.R5,
        [
            _dr(PROVIDER_BALANCE_HELD.spec(r.provider_account_id), r.amount),
            _cr(PROVIDER_BALANCE.spec(r.provider_account_id), r.amount),
        ],
        {"split_id": str(r.split_id)},
    )


def build_r6(ctx: RecipeContext, r: TopUpCaptured) -> tuple[EntryDraft, ...]:
    legs = _capture_legs(
        r.provider_account_id, CUSTOMER_CREDITS.spec(r.customer_id), r.credits, r.gross, r.fee
    )
    return _entry(
        ctx,
        payment_captured_key(r.payment_id),
        RecipeCode.R6,
        legs,
        {"payment_id": str(r.payment_id)},
    )


def _refund_metadata(r: RefundRequest) -> dict[str, JsonValue]:
    return {"refund_id": str(r.refund_id), "payment_id": str(r.payment_id)}


def build_r8_internal(
    ctx: RecipeContext, r: RefundRequested, balances: Mapping[str, Balance]
) -> tuple[EntryDraft, ...]:
    """Shares of the cumulative refund minus shares already refunded (no drift).

    Payee parts are taken from ``available``, then ``pending``, then ``held`` (each up to its
    available balance); the shortfall is owed back by the payee (``payee_receivable``). A negative
    difference (largest-remainder reshuffle of one minor unit) is credited back.
    """
    before = r.original.reallocate(r.already_refunded)
    after = r.original.reallocate(r.already_refunded + r.amount)
    legs = [_signed(PLATFORM_REVENUE.spec(), after.platform - before.platform)]
    for old, new in zip(before.payees, after.payees, strict=True):
        payee = new.payee_id
        diff = new.amount - old.amount
        if diff.is_negative():
            legs.append(_cr(PAYEE_PAYABLE_AVAILABLE.spec(payee), -diff))
            continue
        buckets = [
            template.spec(payee)
            for template in (PAYEE_PAYABLE_AVAILABLE, PAYEE_PAYABLE_PENDING, PAYEE_PAYABLE_HELD)
        ]
        taken, shortfall = _drain(diff, [(b, balances.get(b.code)) for b in buckets])
        legs.extend(_dr(account, amount) for account, amount in taken)
        legs.append(_dr(PAYEE_RECEIVABLE.spec(payee), shortfall))
    legs.append(_cr(r.source, r.amount))
    return _entry(
        ctx, refund_pending_key(r.refund_id), RecipeCode.R8, legs, _refund_metadata(r), pending=True
    )


def build_r8_provider_split(
    ctx: RecipeContext, r: ProviderSplitRefundRequested
) -> tuple[EntryDraft, ...]:
    """Only what leaves our provider account is credited: ``R - sum(done)``.

    Payee parts the provider debited from us (``done=false``) are owed back by the payee; the
    platform bears ``R - sum(all refunded splits)`` (see the report's model question on R8).
    """
    zero = Money.zero(r.amount.currency)
    not_done = [s for s in r.refunded_splits if not s.done]
    done_total = sum((s.amount for s in r.refunded_splits if s.done), zero)
    legs = [
        _dr(PLATFORM_REVENUE.spec(), r.amount - r.splits_total),
        *(_dr(PAYEE_RECEIVABLE.spec(s.payee_id), s.amount) for s in not_done),
        _cr(r.source, r.amount - done_total),
    ]
    return _entry(
        ctx, refund_pending_key(r.refund_id), RecipeCode.R8, legs, _refund_metadata(r), pending=True
    )


def build_r8_top_up(
    ctx: RecipeContext, r: TopUpRefundRequested, balances: Mapping[str, Balance]
) -> tuple[EntryDraft, ...]:
    credits = CUSTOMER_CREDITS.spec(r.customer_id)
    taken, shortfall = _drain(r.amount, [(credits, balances.get(credits.code))])
    legs = [
        *(_dr(account, amount) for account, amount in taken),
        _dr(CUSTOMER_RECEIVABLE.spec(r.customer_id), shortfall),
        _cr(r.source, r.amount),
    ]
    return _entry(
        ctx, refund_pending_key(r.refund_id), RecipeCode.R8, legs, _refund_metadata(r), pending=True
    )


def build_r8_unapplied(
    ctx: RecipeContext, r: UnappliedCashRefundRequested
) -> tuple[EntryDraft, ...]:
    legs = [_dr(CUSTOMER_UNAPPLIED_CASH.spec(r.customer_id), r.amount), _cr(r.source, r.amount)]
    return _entry(
        ctx, refund_pending_key(r.refund_id), RecipeCode.R8, legs, _refund_metadata(r), pending=True
    )


def build_r9_opened(
    ctx: RecipeContext, r: DisputeOpened, balances: Mapping[str, Balance]
) -> tuple[EntryDraft, ...]:
    """Hold ``D`` at the provider and freeze ``d_i`` from each payee (available, then pending).

    What cannot be frozen (payee already paid out) stays unfrozen; if the dispute is lost it is
    owed back by the payee (``payee_receivable``, R9 lost).
    """
    legs = [
        _dr(PROVIDER_BALANCE_HELD.spec(r.provider_account_id), r.amount),
        _cr(PROVIDER_BALANCE.spec(r.provider_account_id), r.amount),
    ]
    for share in r.shares().payees:
        buckets = [
            PAYEE_PAYABLE_AVAILABLE.spec(share.payee_id),
            PAYEE_PAYABLE_PENDING.spec(share.payee_id),
        ]
        taken, _unfrozen = _drain(share.amount, [(b, balances.get(b.code)) for b in buckets])
        legs.extend(_dr(account, amount) for account, amount in taken)
        frozen = sum((amount for _, amount in taken), Money.zero(r.amount.currency))
        legs.append(_cr(PAYEE_PAYABLE_HELD.spec(share.payee_id), frozen))
    return _entry(
        ctx,
        dispute_opened_key(r.dispute_id),
        RecipeCode.R9,
        legs,
        {"dispute_id": str(r.dispute_id)},
    )


def build_r9_lost(
    ctx: RecipeContext, r: DisputeLost, frozen: Mapping[PayeeId, Money]
) -> tuple[EntryDraft, ...]:
    """``frozen``: per payee, the amount the R9 opened entry moved to ``payee_payable_held``."""
    shares = r.shares()
    known = {s.payee_id for s in shares.payees}
    if not set(frozen) <= known:
        raise ValidationFailed("frozen amounts reference payees outside the original parts")
    zero = Money.zero(r.amount.currency)
    legs = [_dr(PLATFORM_REVENUE.spec(), shares.platform)]
    for share in shares.payees:
        held = frozen.get(share.payee_id, zero)
        if held > share.amount or held.is_negative():
            raise ValidationFailed(f"frozen amount of payee {share.payee_id} is not within d_i")
        legs.append(_dr(PAYEE_PAYABLE_HELD.spec(share.payee_id), held))
        legs.append(_dr(PAYEE_RECEIVABLE.spec(share.payee_id), share.amount - held))
    legs.append(_cr(PROVIDER_BALANCE_HELD.spec(r.provider_account_id), r.amount))
    legs.append(_dr(PROVIDER_FEES.spec(), r.chargeback_fee))
    legs.append(_cr(PROVIDER_BALANCE.spec(r.provider_account_id), r.chargeback_fee))
    return _entry(
        ctx,
        _key("dispute", r.dispute_id, "lost"),
        RecipeCode.R9,
        legs,
        {"dispute_id": str(r.dispute_id)},
    )


def build_r10_request(ctx: RecipeContext, r: PayoutRequested) -> tuple[EntryDraft, ...]:
    return _entry(
        ctx,
        payout_pending_key(r.payout_id),
        RecipeCode.R10,
        [
            _dr(PAYEE_PAYABLE_AVAILABLE.spec(r.payee_id), r.amount),
            _cr(PROVIDER_BALANCE.spec(r.provider_account_id), r.amount),
        ],
        {"payout_id": str(r.payout_id), "approval_id": str(r.approval.approval_id)},
        pending=True,
    )


def build_r10_fee(ctx: RecipeContext, r: PayoutPaid) -> tuple[EntryDraft, ...]:
    return _entry(
        ctx,
        _key("payout", r.payout_id, "fee"),
        RecipeCode.R10,
        [
            _dr(PROVIDER_FEES.spec(), r.transfer_fee),
            _cr(PROVIDER_BALANCE.spec(r.provider_account_id), r.transfer_fee),
        ],
        {"payout_id": str(r.payout_id)},
    )


def build_r12(ctx: RecipeContext, r: ReconciliationAdjustment) -> tuple[EntryDraft, ...]:
    return _entry(
        ctx,
        _key("reconciliation", r.adjustment_id, "adjustment"),
        RecipeCode.R12,
        [
            _signed(PROVIDER_BALANCE.spec(r.provider_account_id), r.amount),
            _signed(OPENING_BALANCE.spec(), -r.amount),
        ],
        {
            "adjustment_id": str(r.adjustment_id),
            "reason": r.reason,
            "approval_id": str(r.approval.approval_id),
        },
    )


def build_r13(ctx: RecipeContext, r: WriteOff) -> tuple[EntryDraft, ...]:
    return _entry(
        ctx,
        _key("write_off", r.write_off_id),
        RecipeCode.R13,
        [_dr(CHARGEBACK_LOSSES.spec(), r.amount), _cr(r.target, r.amount)],
        {
            "write_off_id": str(r.write_off_id),
            "reason": r.reason,
            "approval_id": str(r.approval.approval_id),
        },
    )


def balance_accounts(
    r: RefundRequested | TopUpRefundRequested | DisputeOpened,
) -> tuple[AccountSpec, ...]:
    """Accounts whose locked balances a balance-aware recipe reads (and may post to)."""
    match r:
        case RefundRequested():
            payees = [p.payee_id for p in r.original.payees]
            templates = (
                PAYEE_PAYABLE_AVAILABLE,
                PAYEE_PAYABLE_PENDING,
                PAYEE_PAYABLE_HELD,
                PAYEE_RECEIVABLE,
            )
            return tuple(t.spec(p) for p in payees for t in templates)
        case TopUpRefundRequested():
            return (CUSTOMER_CREDITS.spec(r.customer_id), CUSTOMER_RECEIVABLE.spec(r.customer_id))
        case DisputeOpened():
            payees = [p.payee_id for p in r.original.payees]
            templates = (PAYEE_PAYABLE_AVAILABLE, PAYEE_PAYABLE_PENDING, PAYEE_PAYABLE_HELD)
            return tuple(t.spec(p) for p in payees for t in templates)
