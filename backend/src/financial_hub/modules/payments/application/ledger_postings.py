"""Ledger postings of payment transitions (FINANCIAL_MODEL s4: R1-R4, R2a, R2b, R8; PRO-42).

The payment service calls :func:`post_payment_changes` in the transaction that records a
transition, with the provider payment before and after it. The postings are derived from that
difference only, never from provider payloads, so the fake provider and Asaas reach the same code:

- first capture of the intent: **R1** (a standalone intent is its own invoice: ``B`` = the intent
  amount, no wallet) + **R2** (``G`` = captured amount, ``F = G - N`` when the net is known, else
  0) + **R3** (no split: the platform takes ``B``);
- a late capture (the payment had failed or been canceled) or a capture of an intent already
  billed by another provider payment: **R2b** unapplied cash, never R2/R3;
- the reported net changed after the capture: **R2a** (see :func:`with_reported_net`);
- ``settlement_status -> settled``: **R4** for the booked net;
- a hub refund requested: **R8** step 1 (pending hold); refund ``done``/``denied``/``cancelled``:
  **R8** step 2; a refund made at the provider (imported ``done``) is booked directly as posted.

Idempotency: each entry's ledger key derives from the provider payment or refund id and the recipe
(``payment:{id}:captured``, ``payment:{id}:settled``, ``refund:{id}:pending`` ...). Facts already
booked are skipped, so a redelivered webhook, a resolver re-run or a replayed transition never
posts twice; a retried transaction rolls the postings back with the transition.
"""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Final
from uuid import UUID, uuid5

import structlog

from financial_hub.modules.ledger.application import (
    CustomerId,
    FeeAdjusted,
    FeeBearer,
    InvoiceFinalized,
    LedgerInput,
    PaymentCaptured,
    PaymentSettled,
    ProviderAccountId,
    RecipeCode,
    RecipeContext,
    RefundFinished,
    RefundOutcome,
    RefundRequested,
    RefundRequestInput,
    SplitAllocated,
    SplitAllocation,
    SplitRule,
    UnappliedCashReceived,
    UnappliedCashRefundRequested,
    apply_recipes,
    booked_capture,
    fee_adjustment_booked,
    unbooked,
)
from financial_hub.modules.payments.domain import (
    TERMINAL_REFUND_STATES,
    CaptureStatus,
    PaymentIntent,
    PaymentState,
    ProviderPayment,
    Refund,
    RefundState,
    SettlementStatus,
)
from financial_hub.platform.db import UnitOfWork
from financial_hub.shared.errors import ValidationFailed
from financial_hub.shared.money import Money

logger = structlog.get_logger()

# Payments without a provider account (the fake provider) post to one surrogate account per
# provider and project: uuid5(namespace, "{provider}:{project_id}").
SURROGATE_ACCOUNT_NAMESPACE: Final = UUID("8d3f4c1e-5a62-5b7e-9c0d-2f6a1b3e4d57")
# No split rules yet (PRO-42): the platform takes the whole billed amount.
NO_SPLIT: Final = SplitRule(payees=(), platform_weight=1)
_LATE: Final = frozenset({CaptureStatus.FAILED, CaptureStatus.CANCELED})
_NOT_HOLDING: Final = frozenset({RefundState.DENIED, RefundState.CANCELLED})
_OUTCOMES: Final = {
    RefundState.DONE: RefundOutcome.DONE,
    RefundState.DENIED: RefundOutcome.DENIED,
    RefundState.CANCELLED: RefundOutcome.CANCELLED,
}


@dataclass(frozen=True, slots=True)
class PaymentChange:
    """One recorded transition of a provider payment and its refunds.

    ``before`` is the payment state before the transition (``PaymentState()`` when the payment is
    created in this transaction); ``payment`` and ``refunds_after`` are the state after it.
    ``effective_at`` is when the hub recorded the transition.
    """

    intent: PaymentIntent
    payment: ProviderPayment
    before: PaymentState
    effective_at: datetime
    refunds_before: Sequence[Refund] = ()
    refunds_after: Sequence[Refund] = ()


type LedgerHook = Callable[[UnitOfWork, PaymentChange], Awaitable[None]]


async def post_payment_changes(uow: UnitOfWork, change: PaymentChange) -> None:
    """Book the ledger entries of ``change`` in the caller's transaction (one ledger write)."""
    project_id = change.payment.project_id
    booked = await booked_capture(uow, project_id, change.payment.id)
    billed = False
    if booked is None and _newly_captured(change):
        r1 = capture_facts(change.intent, change.payment, unapplied=False)[0]
        billed = not await unbooked(uow, project_id, [r1])
    facts = await unbooked(uow, project_id, plan_postings(change, booked, intent_billed=billed))
    if not facts:
        return
    context = RecipeContext(
        project_id=project_id, effective_at=change.effective_at, correlation_id=change.intent.id
    )
    result = await apply_recipes(uow, facts, context=context)
    logger.info(
        "payments.ledger.posted",
        project_id=str(project_id),
        provider_payment_id=str(change.payment.id),
        recipes=[entry.recipe for entry in result.entries],
    )


def plan_postings(
    change: PaymentChange, booked: RecipeCode | None, *, intent_billed: bool
) -> list[LedgerInput]:
    """The facts of ``change`` in booking order (capture, R2a, R4, refunds); pure.

    ``booked``: how the capture is already in the ledger (``None``: not booked);
    ``intent_billed``: another provider payment of the intent already booked R1.
    """
    intent, payment, before = change.intent, change.payment, change.before
    after = payment.state
    facts: list[LedgerInput] = []
    capture = booked
    if _newly_captured(change) and capture is None:
        unapplied = before.capture_status in _LATE or intent_billed
        facts.extend(capture_facts(intent, payment, unapplied=unapplied))
        capture = RecipeCode.R2B if unapplied else RecipeCode.R2
    if capture is None:
        if after.capture_status is CaptureStatus.CAPTURED and _has_followups(change):
            logger.warning(
                "payments.ledger.capture_not_booked",
                provider_payment_id=str(payment.id),
                detail="captured before the ledger hook existed; reconciliation books it",
            )
        return facts
    adjusted = fee_fact(intent, payment, before)
    if adjusted is not None:
        facts.append(adjusted)
    if (
        before.settlement_status is not SettlementStatus.SETTLED
        and after.settlement_status is SettlementStatus.SETTLED
    ):
        facts.append(settlement_fact(intent, payment))
    facts.extend(refund_facts(change, capture))
    return facts


def capture_facts(
    intent: PaymentIntent, payment: ProviderPayment, *, unapplied: bool
) -> list[LedgerInput]:
    """R1 + R2 + R3 for the first capture of the intent, else R2b."""
    currency = intent.amount.currency
    gross = Money(payment.state.captured_amount, currency)
    fee = gross - Money(booked_net(payment.state), currency)
    customer = CustomerId(intent.customer_id)
    account = provider_account(payment)
    if unapplied:
        return [UnappliedCashReceived(payment.id, customer, account, gross, fee)]
    billed = intent.amount
    return [
        InvoiceFinalized(intent.id, customer, billed, Money.zero(currency)),
        PaymentCaptured(payment.id, customer, account, billed, gross, fee),
        SplitAllocated(payment.id, billed, gross - fee, FeeBearer.PROPORTIONAL, NO_SPLIT),
    ]


def fee_fact(
    intent: PaymentIntent, payment: ProviderPayment, before: PaymentState
) -> FeeAdjusted | None:
    """R2a when the booked net of an already captured payment changed in this transition."""
    after = payment.state
    if (
        before.capture_status is not CaptureStatus.CAPTURED
        or after.capture_status is not CaptureStatus.CAPTURED
        or booked_net(before) == booked_net(after)
    ):
        return None
    currency = intent.amount.currency
    return FeeAdjusted(
        payment_id=payment.id,
        provider_account_id=provider_account(payment),
        gross=Money(after.captured_amount, currency),
        booked_fee=Money(before.captured_amount - booked_net(before), currency),
        net_value=Money(booked_net(after), currency),
    )


def settlement_fact(intent: PaymentIntent, payment: ProviderPayment) -> PaymentSettled:
    """R4: the booked net moves from the provider receivable to the provider balance."""
    net = Money(booked_net(payment.state), intent.amount.currency)
    return PaymentSettled(payment.id, provider_account(payment), net)


def refund_facts(change: PaymentChange, capture: RecipeCode) -> list[LedgerInput]:
    """R8 step 1 for refunds the hub just requested, step 2 for refunds that just finished."""
    previous = {refund.id: refund.status for refund in change.refunds_before}
    facts: list[LedgerInput] = []
    for refund in change.refunds_after:
        was = previous.get(refund.id)
        if was is refund.status or (was is not None and was in TERMINAL_REFUND_STATES):
            continue
        if refund.status not in TERMINAL_REFUND_STATES and was is not None:
            continue  # submitted/unknown/requested again: the hold stays as it is
        try:
            request = refund_request(change, refund, capture)
        except ValidationFailed as error:
            logger.error(
                "payments.ledger.refund_not_booked",
                refund_id=str(refund.id),
                provider_payment_id=str(change.payment.id),
                reason=str(error),
            )
            continue
        outcome = _OUTCOMES.get(refund.status)
        facts.append(request if outcome is None else RefundFinished(request, outcome))
    return facts


def refund_request(
    change: PaymentChange, refund: Refund, capture: RecipeCode
) -> RefundRequestInput:
    """R8 step-1 facts of ``refund``; ``already_refunded`` = earlier refunds that hold money."""
    intent, payment = change.intent, change.payment
    currency = intent.amount.currency
    earlier = 0
    for other in change.refunds_after:  # oldest first, as the repository lists them
        if other.id == refund.id:
            break
        if other.status not in _NOT_HOLDING:
            earlier += other.amount.minor_units
    gross = Money(payment.state.captured_amount, currency)
    already = Money(earlier, currency)
    settled = payment.state.settlement_status is SettlementStatus.SETTLED
    account = provider_account(payment)
    if capture is RecipeCode.R2B:
        return UnappliedCashRefundRequested(
            refund_id=refund.id,
            payment_id=payment.id,
            provider_account_id=account,
            amount=refund.amount,
            gross=gross,
            already_refunded=already,
            settled=settled,
            customer_id=CustomerId(intent.customer_id),
        )
    return RefundRequested(
        refund_id=refund.id,
        payment_id=payment.id,
        provider_account_id=account,
        amount=refund.amount,
        gross=gross,
        already_refunded=already,
        settled=settled,
        original=SplitAllocation(payees=(), platform=intent.amount),
    )


async def with_reported_net(
    uow: UnitOfWork,
    intent: PaymentIntent,
    payment: ProviderPayment,
    state: PaymentState,
    reported: int | None,
) -> PaymentState:
    """``state`` carrying the net the provider now reports, when that is a fee adjustment (R2a).

    Only for a payment captured before this transition (a capture records its own net). A net that
    returns to a value already adjusted to cannot be booked again under its R2a key: it is left
    as it is and logged, and reconciliation (PRO-43) surfaces it.
    """
    if (
        reported is None
        or type(reported) is not int
        or payment.state.capture_status is not CaptureStatus.CAPTURED
        or state.capture_status is not CaptureStatus.CAPTURED
        or not 0 <= reported <= state.captured_amount
        or reported == booked_net(state)
    ):
        return state
    net = Money(reported, intent.amount.currency)
    if await fee_adjustment_booked(uow, payment.project_id, payment.id, net):
        logger.warning(
            "payments.ledger.fee_adjustment_repeated",
            provider_payment_id=str(payment.id),
            net_amount=reported,
        )
        return state
    return replace(state, net_amount=reported, fee_estimated=state.captured_amount - reported)


def booked_net(state: PaymentState) -> int:
    """``N`` as booked: the reported net, or the gross while the provider reports none (fee 0)."""
    return state.captured_amount if state.net_amount is None else state.net_amount


def provider_account(payment: ProviderPayment) -> ProviderAccountId:
    """The payment's provider account, or the provider's surrogate in the project (fake)."""
    if payment.provider_account_id is not None:
        return ProviderAccountId(payment.provider_account_id)
    name = f"{payment.provider}:{payment.project_id}"
    return ProviderAccountId(uuid5(SURROGATE_ACCOUNT_NAMESPACE, name))


def _newly_captured(change: PaymentChange) -> bool:
    return (
        change.before.capture_status is not CaptureStatus.CAPTURED
        and change.payment.state.capture_status is CaptureStatus.CAPTURED
    )


def _has_followups(change: PaymentChange) -> bool:
    """Whether the change carries anything the ledger would book after the capture."""
    before, after = change.before, change.payment.state
    return (
        before.settlement_status is not after.settlement_status
        or booked_net(before) != booked_net(after)
        or list(change.refunds_after) != list(change.refunds_before)
    )
