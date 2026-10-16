"""Payment transitions -> ledger facts (PRO-42): amounts, fee/net combinations, keys, refunds."""

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import UUID, uuid4, uuid5

import pytest
from hypothesis import given
from hypothesis import strategies as st

from financial_hub.modules.ledger.application import (
    CUSTOMER_RECEIVABLE,
    CUSTOMER_UNAPPLIED_CASH,
    DISCOUNTS_GIVEN,
    OTHER_INCOME,
    PLATFORM_REVENUE,
    PROVIDER_BALANCE,
    PROVIDER_FEES,
    PROVIDER_RECEIVABLE,
    SALES_CLEARING,
    Direction,
    EntryDraft,
    FeeAdjusted,
    InvoiceFinalized,
    LedgerInput,
    PaymentCaptured,
    PaymentSettled,
    RecipeCode,
    RecipeContext,
    RefundFinished,
    RefundOutcome,
    RefundRequested,
    SplitAllocated,
    UnappliedCashReceived,
    UnappliedCashRefundRequested,
    recipe_key,
)
from financial_hub.modules.ledger.domain.recipes import (
    build_r1,
    build_r2,
    build_r2a,
    build_r2b,
    build_r3,
    build_r4,
    build_r8_internal,
    build_r8_unapplied,
)
from financial_hub.modules.payments.application.ledger_postings import (
    NO_SPLIT,
    SURROGATE_ACCOUNT_NAMESPACE,
    PaymentChange,
    booked_net,
    capture_facts,
    plan_postings,
    provider_account,
)
from financial_hub.modules.payments.domain import (
    CaptureStatus,
    CreationStatus,
    PaymentIntent,
    PaymentMethod,
    PaymentState,
    ProviderPayment,
    Refund,
    RefundState,
    SettlementStatus,
)
from financial_hub.shared.money import BRL, Money

NOW = datetime(2026, 10, 16, 12, tzinfo=UTC)
PROJECT = uuid4()
CUSTOMER = uuid4()
CAPTURED = PaymentState(CaptureStatus.CAPTURED, captured_amount=10_000)
SETTLED = SettlementStatus.SETTLED


def intent(amount: int = 10_000) -> PaymentIntent:
    return PaymentIntent(
        id=uuid4(),
        project_id=PROJECT,
        customer_id=CUSTOMER,
        amount=Money(amount, BRL),
        method=PaymentMethod.PIX,
        due_date=date(2026, 10, 20),
        external_ref=None,
        description=None,
        metadata={},
        provider="fake",
        provider_account_id=None,
        creation_status=CreationStatus.CREATED,
        attempt_id=uuid4(),
        failure_code=None,
        created_at=NOW,
        updated_at=NOW,
    )


def payment(of: PaymentIntent, state: PaymentState, account: UUID | None = None) -> ProviderPayment:
    return ProviderPayment(
        id=uuid4(),
        payment_intent_id=of.id,
        project_id=PROJECT,
        provider="fake",
        provider_account_id=account,
        provider_payment_id="pay_1",
        raw_status="x",
        state=state,
        created_at=NOW,
        updated_at=NOW,
    )


def refund(
    of: ProviderPayment, amount: int, status: RefundState, *, minute: int = 0, origin: str = "hub"
) -> Refund:
    return Refund(
        id=uuid4(),
        project_id=PROJECT,
        payment_intent_id=of.payment_intent_id,
        provider_payment_id=of.id,
        amount=Money(amount, BRL),
        status=status,
        reason=None,
        provider_refund_id=None,
        attempt_id=uuid4() if origin == "hub" else None,
        origin=origin,
        created_at=NOW.replace(minute=minute),
        updated_at=NOW,
    )


def change(
    of: PaymentIntent,
    after: ProviderPayment,
    before: PaymentState,
    refunds_before: Sequence[Refund] = (),
    refunds_after: Sequence[Refund] = (),
) -> PaymentChange:
    return PaymentChange(of, after, before, NOW, refunds_before, refunds_after)


CTX = RecipeContext(PROJECT, NOW)


def entries(fact: LedgerInput) -> tuple[EntryDraft, ...]:
    match fact:
        case InvoiceFinalized():
            return build_r1(CTX, fact)
        case PaymentCaptured():
            return build_r2(CTX, fact)
        case FeeAdjusted():
            return build_r2a(CTX, fact)
        case UnappliedCashReceived():
            return build_r2b(CTX, fact)
        case SplitAllocated():
            return build_r3(CTX, fact)
        case PaymentSettled():
            return build_r4(CTX, fact)
        case RefundRequested():
            return build_r8_internal(CTX, fact, {})
        case UnappliedCashRefundRequested():
            return build_r8_unapplied(CTX, fact)
        case _:
            raise AssertionError(f"unexpected fact {fact!r}")


def legs(facts: Sequence[LedgerInput]) -> dict[tuple[str, Direction], int]:
    """Non-zero amounts per (account code, direction) over every entry of ``facts``."""
    out: dict[tuple[str, Direction], int] = {}
    for fact in facts:
        for entry in entries(fact):
            for leg in entry.postings:
                key = (leg.account.code, leg.direction)
                out[key] = out.get(key, 0) + leg.amount.minor_units
    return {key: amount for key, amount in out.items() if amount}


def net_by_account(facts: Sequence[LedgerInput]) -> dict[str, int]:
    out: dict[str, int] = {}
    for (code, direction), amount in legs(facts).items():
        out[code] = out.get(code, 0) + (amount if direction is Direction.DEBIT else -amount)
    return {code: amount for code, amount in out.items() if amount}


DR, CR = Direction.DEBIT, Direction.CREDIT


# --- capture: R1 + R2 + R3 ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("billed", "gross", "net", "fee"),
    [
        (10_000, 10_000, 10_000, 0),  # zero fee reported
        (10_000, 10_000, None, 0),  # net unknown: fee 0 until R2a
        (10_000, 10_000, 9_701, 299),  # fee known at capture
        (10_000, 10_250, 9_950, 300),  # paid more than billed (interest/fine): other income
        (10_000, 9_800, 9_500, 300),  # paid less (discount)
        (10_000, 10_000, 0, 10_000),  # the fee takes everything
    ],
)
def test_first_capture_books_r1_r2_r3(billed: int, gross: int, net: int | None, fee: int) -> None:
    pi = intent(billed)
    state = PaymentState(CaptureStatus.CAPTURED, captured_amount=gross, net_amount=net)
    pp = payment(pi, state)
    facts = plan_postings(change(pi, pp, PaymentState()), None, intent_billed=False)

    assert [type(f) for f in facts] == [InvoiceFinalized, PaymentCaptured, SplitAllocated]
    r2 = facts[1]
    assert isinstance(r2, PaymentCaptured)
    assert (r2.billed.minor_units, r2.gross.minor_units, r2.fee.minor_units) == (billed, gross, fee)
    r3 = facts[2]
    assert isinstance(r3, SplitAllocated) and r3.split == NO_SPLIT
    assert r3.net.minor_units == gross - fee
    account = provider_account(pp)
    customer = CUSTOMER_RECEIVABLE.spec(CUSTOMER).code
    receivable = PROVIDER_RECEIVABLE.spec(account).code
    # Customer receivable and sales clearing net to zero; the platform earns B.
    assert net_by_account(facts) == {
        k: v
        for k, v in {
            receivable: gross - fee,
            PROVIDER_FEES.spec().code: fee,
            PLATFORM_REVENUE.spec().code: -billed,
            OTHER_INCOME.spec().code: -max(gross - billed, 0),
            DISCOUNTS_GIVEN.spec().code: max(billed - gross, 0),
        }.items()
        if v
    }
    assert customer not in net_by_account(facts)
    assert SALES_CLEARING.spec().code not in net_by_account(facts)


@pytest.mark.parametrize("previous", [CaptureStatus.FAILED, CaptureStatus.CANCELED])
def test_late_capture_books_unapplied_cash_only(previous: CaptureStatus) -> None:
    pi = intent()
    pp = payment(pi, replace(CAPTURED, net_amount=9_900))
    facts = plan_postings(change(pi, pp, PaymentState(previous)), None, intent_billed=False)

    assert len(facts) == 1 and isinstance(facts[0], UnappliedCashReceived)
    assert net_by_account(facts) == {
        PROVIDER_RECEIVABLE.spec(provider_account(pp)).code: 9_900,
        PROVIDER_FEES.spec().code: 100,
        CUSTOMER_UNAPPLIED_CASH.spec(CUSTOMER).code: -10_000,
    }


def test_a_second_capture_of_a_billed_intent_books_unapplied_cash() -> None:
    pi = intent()
    pp = payment(pi, CAPTURED)
    facts = plan_postings(change(pi, pp, PaymentState()), None, intent_billed=True)
    assert [type(f) for f in facts] == [UnappliedCashReceived]


def test_a_capture_already_booked_is_not_booked_again() -> None:
    pi = intent()
    pp = payment(pi, CAPTURED)
    for booked in (RecipeCode.R2, RecipeCode.R2B):
        assert plan_postings(change(pi, pp, PaymentState()), booked, intent_billed=True) == []


def test_nothing_is_booked_before_the_capture() -> None:
    pi = intent()
    for status in (CaptureStatus.PENDING, CaptureStatus.FAILED, CaptureStatus.CANCELED):
        pp = payment(pi, PaymentState(status))
        assert plan_postings(change(pi, pp, PaymentState()), None, intent_billed=False) == []


# --- R2a and R4 -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("before_net", "after_net", "delta"),
    [(None, 9_700, 300), (9_700, 9_800, -100), (9_800, None, -200), (9_700, 9_700, 0)],
)
def test_fee_adjustment(before_net: int | None, after_net: int | None, delta: int) -> None:
    pi = intent()
    pp = payment(pi, replace(CAPTURED, net_amount=after_net))
    before = replace(CAPTURED, net_amount=before_net)
    facts = plan_postings(change(pi, pp, before), RecipeCode.R2, intent_billed=True)

    if delta == 0:
        assert facts == []
        return
    assert len(facts) == 1 and isinstance(facts[0], FeeAdjusted)
    assert facts[0].net_value.minor_units == booked_net(pp.state)
    assert net_by_account(facts) == {
        PROVIDER_FEES.spec().code: delta,
        PROVIDER_RECEIVABLE.spec(provider_account(pp)).code: -delta,
    }


def test_settlement_moves_the_booked_net() -> None:
    pi = intent()
    pp = payment(pi, replace(CAPTURED, net_amount=9_700, settlement_status=SETTLED))
    facts = plan_postings(
        change(pi, pp, replace(CAPTURED, net_amount=9_700)), RecipeCode.R2, intent_billed=True
    )

    assert [type(f) for f in facts] == [PaymentSettled]
    account = provider_account(pp)
    assert net_by_account(facts) == {
        PROVIDER_BALANCE.spec(account).code: 9_700,
        PROVIDER_RECEIVABLE.spec(account).code: -9_700,
    }


def test_capture_fee_change_and_settlement_in_one_transition_keep_their_order() -> None:
    pi = intent()
    pp = payment(pi, replace(CAPTURED, net_amount=9_500, settlement_status=SETTLED))
    facts = plan_postings(change(pi, pp, PaymentState()), None, intent_billed=False)
    assert [type(f) for f in facts] == [
        InvoiceFinalized,
        PaymentCaptured,
        SplitAllocated,
        PaymentSettled,
    ]
    settled = facts[-1]
    assert isinstance(settled, PaymentSettled) and settled.net.minor_units == 9_500
    # Everything collected ends in the provider balance (minus the fee), nothing in receivables.
    assert PROVIDER_RECEIVABLE.spec(provider_account(pp)).code not in net_by_account(facts)


def test_a_settled_payment_is_not_settled_twice() -> None:
    pi = intent()
    state = replace(CAPTURED, settlement_status=SETTLED)
    pp = payment(pi, state)
    assert plan_postings(change(pi, pp, state), RecipeCode.R2, intent_billed=True) == []


# --- R8 -------------------------------------------------------------------------------------------


def test_a_requested_refund_books_a_hold_from_the_receivable_or_the_balance() -> None:
    pi = intent()
    for settlement, source in (
        (SettlementStatus.NONE, PROVIDER_RECEIVABLE),
        (SETTLED, PROVIDER_BALANCE),
    ):
        state = replace(CAPTURED, settlement_status=settlement)
        pp = payment(pi, state)
        first = refund(pp, 3_000, RefundState.DONE, minute=1)
        new = refund(pp, 2_500, RefundState.REQUESTED, minute=2)
        facts = plan_postings(
            change(pi, pp, state, [first], [first, new]), RecipeCode.R2, intent_billed=True
        )
        assert len(facts) == 1
        request = facts[0]
        assert isinstance(request, RefundRequested)
        assert request.already_refunded.minor_units == 3_000 and request.amount.minor_units == 2_500
        assert request.settled is (settlement is SETTLED)
        (entry,) = entries(request)
        assert entry.status.value == "pending"
        assert net_by_account([request]) == {
            PLATFORM_REVENUE.spec().code: 2_500,
            source.spec(provider_account(pp)).code: -2_500,
        }


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (RefundState.DONE, RefundOutcome.DONE),
        (RefundState.DENIED, RefundOutcome.DENIED),
        (RefundState.CANCELLED, RefundOutcome.CANCELLED),
    ],
)
def test_a_finished_refund_completes_its_hold(status: RefundState, outcome: RefundOutcome) -> None:
    pi = intent()
    pp = payment(pi, CAPTURED)
    open_ = refund(pp, 4_000, RefundState.SUBMITTED)
    done = replace(open_, status=status)
    facts = plan_postings(
        change(pi, pp, CAPTURED, [open_], [done]), RecipeCode.R2, intent_billed=True
    )
    assert len(facts) == 1
    finished = facts[0]
    assert isinstance(finished, RefundFinished) and finished.outcome is outcome
    assert finished.request.refund_id == open_.id


def test_open_refund_moves_book_nothing() -> None:
    pi = intent()
    pp = payment(pi, CAPTURED)
    requested = refund(pp, 4_000, RefundState.REQUESTED)
    for target in (RefundState.SUBMITTED, RefundState.UNKNOWN):
        moved = replace(requested, status=target)
        assert (
            plan_postings(
                change(pi, pp, CAPTURED, [requested], [moved]), RecipeCode.R2, intent_billed=True
            )
            == []
        )


def test_a_provider_refund_is_booked_as_finished() -> None:
    pi = intent()
    pp = payment(pi, CAPTURED)
    denied = refund(pp, 9_000, RefundState.DENIED, minute=1)
    imported = refund(pp, 10_000, RefundState.DONE, minute=2, origin="provider")
    facts = plan_postings(
        change(pi, pp, CAPTURED, [denied], [denied, imported]), RecipeCode.R2, intent_billed=True
    )
    assert len(facts) == 1
    finished = facts[0]
    assert isinstance(finished, RefundFinished) and finished.outcome is RefundOutcome.DONE
    # A denied refund holds nothing: the full amount is still refundable.
    assert finished.request.already_refunded.minor_units == 0


def test_a_refund_of_unapplied_cash_debits_the_customer_unapplied_cash() -> None:
    pi = intent()
    pp = payment(pi, CAPTURED)
    new = refund(pp, 1_000, RefundState.REQUESTED)
    facts = plan_postings(change(pi, pp, CAPTURED, [], [new]), RecipeCode.R2B, intent_billed=True)
    assert len(facts) == 1 and isinstance(facts[0], UnappliedCashRefundRequested)
    assert net_by_account(facts) == {
        CUSTOMER_UNAPPLIED_CASH.spec(CUSTOMER).code: 1_000,
        PROVIDER_RECEIVABLE.spec(provider_account(pp)).code: -1_000,
    }


def test_a_refund_above_the_gross_is_not_booked() -> None:
    pi = intent()
    pp = payment(pi, CAPTURED)
    first = refund(pp, 10_000, RefundState.DONE, minute=1)
    extra = refund(pp, 1, RefundState.DONE, minute=2, origin="provider")
    facts = plan_postings(
        change(pi, pp, CAPTURED, [first], [first, extra]), RecipeCode.R2, intent_billed=True
    )
    assert facts == []


def test_a_capture_not_in_the_ledger_books_no_followups() -> None:
    pi = intent()
    state = replace(CAPTURED, settlement_status=SETTLED)
    pp = payment(pi, state)
    assert plan_postings(change(pi, pp, CAPTURED), None, intent_billed=True) == []


# --- keys and accounts ----------------------------------------------------------------------------


def test_every_fact_is_keyed_like_the_entry_it_books() -> None:
    pi = intent()
    pp = payment(pi, replace(CAPTURED, net_amount=9_000, settlement_status=SETTLED))
    requested = refund(pp, 1_000, RefundState.REQUESTED)
    facts: list[LedgerInput] = [
        *plan_postings(change(pi, pp, PaymentState(), [], [requested]), None, intent_billed=False),
        *capture_facts(pi, pp, unapplied=True),
        *plan_postings(
            change(pi, pp, replace(pp.state, net_amount=9_500), [requested], [requested]),
            RecipeCode.R2,
            intent_billed=True,
        ),
    ]
    for fact in facts:
        (entry,) = entries(fact)
        assert recipe_key(fact) == entry.idempotency_key
    assert {type(fact) for fact in facts} == {
        InvoiceFinalized,
        PaymentCaptured,
        SplitAllocated,
        PaymentSettled,
        RefundRequested,
        UnappliedCashReceived,
        FeeAdjusted,
    }
    request = facts[4]
    assert isinstance(request, RefundRequested)
    assert recipe_key(RefundFinished(request, RefundOutcome.DONE)) is None  # a transition


def test_the_provider_account_or_a_stable_surrogate() -> None:
    pi = intent()
    real = uuid4()
    assert provider_account(payment(pi, CAPTURED, real)) == real
    surrogate = provider_account(payment(pi, CAPTURED))
    assert surrogate == uuid5(SURROGATE_ACCOUNT_NAMESPACE, f"fake:{PROJECT}")
    assert provider_account(payment(intent(), CAPTURED)) == surrogate  # same project and provider


@given(
    gross=st.integers(min_value=1, max_value=10**12),
    fee=st.integers(min_value=0, max_value=10**12),
    billed=st.integers(min_value=1, max_value=10**12),
    known=st.booleans(),
)
def test_capture_entries_balance_and_keep_int_minor_units(
    gross: int, fee: int, billed: int, known: bool
) -> None:
    fee = min(fee, gross)
    pi = intent(billed)
    state = PaymentState(
        CaptureStatus.CAPTURED, captured_amount=gross, net_amount=gross - fee if known else None
    )
    pp = payment(pi, state)
    facts = plan_postings(change(pi, pp, PaymentState()), None, intent_billed=False)
    totals = legs(facts)
    debit = sum(v for (_, d), v in totals.items() if d is DR)
    credit = sum(v for (_, d), v in totals.items() if d is CR)
    assert debit == credit
    assert all(type(v) is int for v in totals.values())
    expected_fee = fee if known else 0
    assert net_by_account(facts).get(PROVIDER_FEES.spec().code, 0) == expected_fee
