"""Pure recipe builders (FINANCIAL_MODEL s4): exact accounts, sides and amounts; validation."""

from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

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
    AccountTemplate,
    CustomerId,
    PayeeId,
    ProviderAccountId,
)
from financial_hub.modules.ledger.domain.model import (
    Balance,
    EntryDraft,
    EntryStatus,
    NormalBalance,
)
from financial_hub.modules.ledger.domain.recipes import (
    Approval,
    DisputeLost,
    DisputeOpened,
    FeeAdjusted,
    FeeBearer,
    InvoiceFinalized,
    PayeeAmount,
    PayeeBucket,
    PaymentCaptured,
    PaymentSettled,
    PayoutPaid,
    PayoutRequested,
    ProviderSplitBlocked,
    ProviderSplitDone,
    ProviderSplitRefundRequested,
    RecipeContext,
    ReconciliationAdjustment,
    RefundedSplit,
    RefundRequested,
    SplitAllocated,
    SplitAllocation,
    SplitRule,
    SplitShare,
    TopUpCaptured,
    TopUpRefundRequested,
    UnappliedCashReceived,
    UnappliedCashRefundRequested,
    WalletConsumed,
    WriteOff,
    balance_accounts,
    build_r1,
    build_r2,
    build_r2a,
    build_r2b,
    build_r3,
    build_r4,
    build_r5_blocked,
    build_r5_done,
    build_r6,
    build_r7,
    build_r8_internal,
    build_r8_provider_split,
    build_r8_top_up,
    build_r8_unapplied,
    build_r9_lost,
    build_r9_opened,
    build_r10_fee,
    build_r10_request,
    build_r12,
    build_r13,
    dispute_opened_key,
    invoice_finalized_key,
    invoice_wallet_key,
    payment_captured_key,
    payout_pending_key,
    refund_pending_key,
    split_blocked_key,
)
from financial_hub.shared.errors import ValidationFailed
from financial_hub.shared.money import BRL, USD, Money
from tests.integration.ledger.support import AT

PROJECT = UUID(int=100)
CTX = RecipeContext(PROJECT, AT, correlation_id=UUID(int=101), causation_id=UUID(int=102))
C = CustomerId(UUID(int=1))
PA = ProviderAccountId(UUID(int=2))
DOC = PayeeId(UUID(int=3))
NURSE = PayeeId(UUID(int=4))
FACT = UUID(int=50)
APPROVAL = Approval(UUID(int=60), requested_by=UUID(int=61), approved_by=UUID(int=62))
DOCTOR_75 = SplitRule((SplitShare(DOC, 75),), 25)
# R3 credited parts of the s5 example: doctor 14851, platform B - 14851 = 5149.
GOLDEN_PARTS = SplitAllocation((PayeeAmount(DOC, Money(14851, BRL)),), Money(5149, BRL))

type Leg = tuple[str, str, int]


def brl(units: int) -> Money:
    return Money(units, BRL)


def dr(template: AccountTemplate, units: int, owner: UUID | None = None) -> Leg:
    return (template.code(owner), "debit", units)


def cr(template: AccountTemplate, units: int, owner: UUID | None = None) -> Leg:
    return (template.code(owner), "credit", units)


def only(drafts: tuple[EntryDraft, ...]) -> EntryDraft:
    assert len(drafts) == 1
    return drafts[0]


def legs(drafts: tuple[EntryDraft, ...]) -> list[Leg]:
    return sorted(
        (p.account.code, p.direction.value, p.amount.minor_units) for p in only(drafts).postings
    )


def expect(*items: Leg) -> list[Leg]:
    return sorted(items)


def credit_balance(posted: int = 0, pending_dr: int = 0) -> Balance:
    return Balance(BRL, NormalBalance.CREDIT, posted_cr=posted, pending_dr=pending_dr)


def payee_balances(
    payee: UUID, *, available: int = 0, pending: int = 0, held: int = 0
) -> dict[str, Balance]:
    return {
        PAYEE_PAYABLE_AVAILABLE.code(payee): credit_balance(available),
        PAYEE_PAYABLE_PENDING.code(payee): credit_balance(pending),
        PAYEE_PAYABLE_HELD.code(payee): credit_balance(held),
    }


def refund(**overrides: Any) -> RefundRequested:
    values: dict[str, Any] = {
        "refund_id": FACT,
        "payment_id": UUID(int=51),
        "provider_account_id": PA,
        "amount": brl(20000),
        "gross": brl(20000),
        "already_refunded": brl(0),
        "settled": True,
        "original": GOLDEN_PARTS,
    }
    return RefundRequested(**(values | overrides))


# --- value types ----------------------------------------------------------------------------------


def test_approval_requires_four_eyes() -> None:
    with pytest.raises(ValidationFailed, match="four-eyes"):
        Approval(UUID(int=1), requested_by=UUID(int=2), approved_by=UUID(int=2))
    with pytest.raises(TypeError, match="approved_by"):
        Approval(UUID(int=1), UUID(int=2), "someone")  # pyright: ignore[reportArgumentType]


def test_payee_amount_is_non_negative_money() -> None:
    with pytest.raises(ValidationFailed):
        PayeeAmount(DOC, brl(-1))
    with pytest.raises(TypeError):
        PayeeAmount(DOC, 1)  # pyright: ignore[reportArgumentType]


def test_split_rule_allocates_with_platform_last() -> None:
    allocation = DOCTOR_75.allocate(brl(19801))
    assert allocation == SplitAllocation((PayeeAmount(DOC, brl(14851)),), brl(4950))
    assert allocation.total == brl(19801)
    assert allocation.payee_total == brl(14851)
    assert allocation.weights() == [14851, 4950]
    # Exact tie (0.5 each): the lower index wins, so the platform (last) gets the smaller part.
    tie = SplitRule((SplitShare(DOC, 1),), 1).allocate(brl(3))
    assert (tie.payees[0].amount, tie.platform) == (brl(2), brl(1))
    decimal = SplitRule((SplitShare(DOC, Decimal("12.5")),), Decimal("87.5")).allocate(brl(1000))
    assert decimal.payees[0].amount == brl(125)


@pytest.mark.parametrize(
    ("payees", "platform"),
    [
        ((SplitShare(DOC, 1), SplitShare(DOC, 2)), 1),
        ((SplitShare(DOC, 0.5),), 1),  # pyright: ignore[reportArgumentType]
        ((SplitShare(DOC, -1),), 2),
        ((SplitShare(DOC, 0),), 0),
    ],
)
def test_split_rule_rejects_invalid_weights(payees: tuple[SplitShare, ...], platform: int) -> None:
    with pytest.raises(ValidationFailed):
        SplitRule(payees, platform)


def test_split_rule_rejects_negative_basis() -> None:
    with pytest.raises(ValidationFailed):
        DOCTOR_75.allocate(brl(-1))


def test_split_allocation_validation() -> None:
    with pytest.raises(ValidationFailed, match="platform"):
        SplitAllocation((), brl(-1))
    with pytest.raises(ValidationFailed, match="twice"):
        SplitAllocation((PayeeAmount(DOC, brl(1)), PayeeAmount(DOC, brl(2))), brl(0))
    with pytest.raises(ValidationFailed, match="one currency"):
        SplitAllocation((PayeeAmount(DOC, Money(1, USD)),), brl(1))
    with pytest.raises(ValidationFailed, match="sum to zero"):
        SplitAllocation((), brl(0)).reallocate(brl(1))


def test_idempotency_keys_derive_from_the_fact() -> None:
    assert invoice_finalized_key(FACT) == f"invoice:{FACT}:finalized"
    assert invoice_wallet_key(FACT) == f"invoice:{FACT}:wallet"
    assert payment_captured_key(FACT) == f"payment:{FACT}:captured"
    assert refund_pending_key(FACT) == f"refund:{FACT}:pending"
    assert dispute_opened_key(FACT) == f"dispute:{FACT}:opened"
    assert payout_pending_key(FACT) == f"payout:{FACT}:pending"
    assert split_blocked_key(FACT) == f"split:{FACT}:blocked"


# --- R1 / R7 --------------------------------------------------------------------------------------


def test_r1_books_amount_due_after_wallet() -> None:
    drafts = build_r1(CTX, InvoiceFinalized(FACT, C, brl(20000), brl(5000)))
    assert legs(drafts) == expect(dr(CUSTOMER_RECEIVABLE, 15000, C), cr(SALES_CLEARING, 15000))
    draft = only(drafts)
    assert (draft.idempotency_key, draft.recipe, draft.status) == (
        f"invoice:{FACT}:finalized",
        "R1",
        EntryStatus.POSTED,
    )
    assert dict(draft.metadata) == {"invoice_id": str(FACT)}
    assert (draft.project_id, draft.effective_at) == (PROJECT, AT)
    assert (draft.correlation_id, draft.causation_id) == (UUID(int=101), UUID(int=102))


def test_r1_fully_paid_by_wallet_creates_no_entry() -> None:
    assert build_r1(CTX, InvoiceFinalized(FACT, C, brl(5000), brl(5000))) == ()


@pytest.mark.parametrize(
    ("sales", "wallet", "error"),
    [
        (brl(100), brl(101), "exceed"),
        (brl(100), brl(-1), ">= 0"),
        (brl(100), Money(1, USD), "one currency"),
    ],
)
def test_r1_validation(sales: Money, wallet: Money, error: str) -> None:
    with pytest.raises(ValidationFailed, match=error):
        InvoiceFinalized(FACT, C, sales, wallet)


def test_r7_wallet_consumption_credits_available_shares() -> None:
    drafts = build_r7(CTX, WalletConsumed(FACT, C, brl(10000), DOCTOR_75))
    assert legs(drafts) == expect(
        dr(CUSTOMER_CREDITS, 10000, C),
        cr(SALES_CLEARING, 10000),
        dr(SALES_CLEARING, 10000),
        cr(PAYEE_PAYABLE_AVAILABLE, 7500, DOC),
        cr(PLATFORM_REVENUE, 2500),
    )
    assert only(drafts).idempotency_key == f"invoice:{FACT}:wallet"
    assert build_r7(CTX, WalletConsumed(FACT, C, brl(0), DOCTOR_75)) == ()
    with pytest.raises(ValidationFailed):
        WalletConsumed(FACT, C, brl(-1), DOCTOR_75)


# --- R2 / R2a / R2b / R3 --------------------------------------------------------------------------


def test_r2_capture_with_fee() -> None:
    captured = PaymentCaptured(FACT, C, PA, brl(20000), brl(20000), brl(199))
    assert captured.net == brl(19801)
    drafts = build_r2(CTX, captured)
    assert legs(drafts) == expect(
        dr(PROVIDER_RECEIVABLE, 20000, PA),
        cr(CUSTOMER_RECEIVABLE, 20000, C),
        dr(PROVIDER_FEES, 199),
        cr(PROVIDER_RECEIVABLE, 199, PA),
    )
    assert only(drafts).idempotency_key == f"payment:{FACT}:captured"


def test_r2_interest_goes_to_other_income_and_discount_to_discounts_given() -> None:
    interest = build_r2(CTX, PaymentCaptured(FACT, C, PA, brl(20000), brl(20500), brl(0)))
    assert legs(interest) == expect(
        dr(PROVIDER_RECEIVABLE, 20500, PA),
        cr(CUSTOMER_RECEIVABLE, 20000, C),
        cr(OTHER_INCOME, 500),
    )
    discount = build_r2(CTX, PaymentCaptured(FACT, C, PA, brl(20000), brl(19000), brl(0)))
    assert legs(discount) == expect(
        dr(PROVIDER_RECEIVABLE, 19000, PA),
        cr(CUSTOMER_RECEIVABLE, 20000, C),
        dr(DISCOUNTS_GIVEN, 1000),
    )


@pytest.mark.parametrize(
    ("billed", "gross", "fee", "error"),
    [
        (100, 100, 101, "fee cannot exceed"),
        (100, 0, 0, "gross must be > 0"),
        (100, 100, -1, "fee must be >= 0"),
        (-1, 100, 0, "billed must be >= 0"),
    ],
)
def test_r2_validation(billed: int, gross: int, fee: int, error: str) -> None:
    with pytest.raises(ValidationFailed, match=error):
        PaymentCaptured(FACT, C, PA, brl(billed), brl(gross), brl(fee))


def test_r2a_posts_fee_delta_both_ways_and_nothing_when_unchanged() -> None:
    higher = build_r2a(CTX, FeeAdjusted(FACT, PA, brl(20000), brl(199), brl(19750)))
    assert legs(higher) == expect(dr(PROVIDER_FEES, 51), cr(PROVIDER_RECEIVABLE, 51, PA))
    assert only(higher).idempotency_key == f"payment:{FACT}:fee:19750"
    lower = build_r2a(CTX, FeeAdjusted(FACT, PA, brl(20000), brl(199), brl(19850)))
    assert legs(lower) == expect(cr(PROVIDER_FEES, 49), dr(PROVIDER_RECEIVABLE, 49, PA))
    assert build_r2a(CTX, FeeAdjusted(FACT, PA, brl(20000), brl(199), brl(19801))) == ()


@pytest.mark.parametrize(("booked", "net"), [(199, 20001), (20001, 100), (-1, 100), (0, -1)])
def test_r2a_validation(booked: int, net: int) -> None:
    with pytest.raises(ValidationFailed):
        FeeAdjusted(FACT, PA, brl(20000), brl(booked), brl(net))


def test_r2b_unapplied_cash() -> None:
    drafts = build_r2b(CTX, UnappliedCashReceived(FACT, C, PA, brl(5000), brl(100)))
    assert legs(drafts) == expect(
        dr(PROVIDER_RECEIVABLE, 5000, PA),
        cr(CUSTOMER_UNAPPLIED_CASH, 5000, C),
        dr(PROVIDER_FEES, 100),
        cr(PROVIDER_RECEIVABLE, 100, PA),
    )
    assert only(drafts).idempotency_key == f"payment:{FACT}:unapplied"


def test_r3_golden_split_proportional() -> None:
    allocated = SplitAllocated(FACT, brl(20000), brl(19801), FeeBearer.PROPORTIONAL, DOCTOR_75)
    assert allocated.basis == brl(19801)
    assert allocated.allocation().payees == (PayeeAmount(DOC, brl(14851)),)
    drafts = build_r3(CTX, allocated)
    assert legs(drafts) == expect(
        dr(SALES_CLEARING, 20000),
        cr(PAYEE_PAYABLE_PENDING, 14851, DOC),
        cr(PLATFORM_REVENUE, 5149),
    )
    assert dict(only(drafts).metadata) == {"payment_id": str(FACT), "fee_bearer": "proportional"}
    assert only(drafts).idempotency_key == f"payment:{FACT}:split"


def test_r3_platform_bears_fee() -> None:
    allocated = SplitAllocated(FACT, brl(20000), brl(19801), FeeBearer.PLATFORM, DOCTOR_75)
    assert legs(build_r3(CTX, allocated)) == expect(
        dr(SALES_CLEARING, 20000),
        cr(PAYEE_PAYABLE_PENDING, 15000, DOC),
        cr(PLATFORM_REVENUE, 5000),
    )


def test_r3_rejects_shares_above_net_or_billed() -> None:
    everything = SplitRule((SplitShare(DOC, 1),), 0)
    with pytest.raises(ValidationFailed, match="net value"):
        SplitAllocated(FACT, brl(20000), brl(19801), FeeBearer.PLATFORM, everything)
    # N > B (interest collected): shares over N would exceed what the customer was billed.
    with pytest.raises(ValidationFailed, match="billed"):
        SplitAllocated(FACT, brl(20000), brl(20300), FeeBearer.PROPORTIONAL, everything)
    with pytest.raises(TypeError):
        SplitAllocated(
            FACT,
            brl(1),
            brl(1),
            "proportional",  # pyright: ignore[reportArgumentType]
            DOCTOR_75,
        )


# --- R4 / R5 / R6 ---------------------------------------------------------------------------------


def test_r4_settlement_moves_internal_shares_to_available() -> None:
    settled = PaymentSettled(FACT, PA, brl(19801), (PayeeAmount(DOC, brl(14851)),))
    drafts = build_r4(CTX, settled)
    assert legs(drafts) == expect(
        dr(PROVIDER_BALANCE, 19801, PA),
        cr(PROVIDER_RECEIVABLE, 19801, PA),
        dr(PAYEE_PAYABLE_PENDING, 14851, DOC),
        cr(PAYEE_PAYABLE_AVAILABLE, 14851, DOC),
    )
    assert only(drafts).idempotency_key == f"payment:{FACT}:settled"
    assert legs(build_r4(CTX, PaymentSettled(FACT, PA, brl(9901)))) == expect(
        dr(PROVIDER_BALANCE, 9901, PA), cr(PROVIDER_RECEIVABLE, 9901, PA)
    )
    with pytest.raises(ValidationFailed, match="twice"):
        PaymentSettled(FACT, PA, brl(2), (PayeeAmount(DOC, brl(1)), PayeeAmount(DOC, brl(1))))


def test_r5_provider_split_done_and_blocked() -> None:
    done = build_r5_done(CTX, ProviderSplitDone(FACT, DOC, PA, brl(5000)))
    assert legs(done) == expect(
        dr(PAYEE_PAYABLE_PENDING, 5000, DOC), cr(PROVIDER_BALANCE, 5000, PA)
    )
    assert only(done).idempotency_key == f"split:{FACT}:done"
    available = build_r5_done(
        CTX, ProviderSplitDone(FACT, DOC, PA, brl(5000), PayeeBucket.AVAILABLE)
    )
    assert legs(available) == expect(
        dr(PAYEE_PAYABLE_AVAILABLE, 5000, DOC), cr(PROVIDER_BALANCE, 5000, PA)
    )
    blocked = build_r5_blocked(CTX, ProviderSplitBlocked(FACT, PA, brl(700)))
    assert legs(blocked) == expect(
        dr(PROVIDER_BALANCE_HELD, 700, PA), cr(PROVIDER_BALANCE, 700, PA)
    )
    with pytest.raises(ValidationFailed):
        ProviderSplitDone(FACT, DOC, PA, brl(0))


def test_r6_top_up() -> None:
    drafts = build_r6(CTX, TopUpCaptured(FACT, C, PA, brl(10000), brl(10000), brl(99)))
    assert legs(drafts) == expect(
        dr(PROVIDER_RECEIVABLE, 10000, PA),
        cr(CUSTOMER_CREDITS, 10000, C),
        dr(PROVIDER_FEES, 99),
        cr(PROVIDER_RECEIVABLE, 99, PA),
    )
    assert (only(drafts).recipe, only(drafts).idempotency_key) == (
        "R6",
        f"payment:{FACT}:captured",
    )


# --- R8 -------------------------------------------------------------------------------------------


def test_r8_internal_full_refund_is_pending() -> None:
    drafts = build_r8_internal(CTX, refund(), payee_balances(DOC, available=14851))
    assert legs(drafts) == expect(
        dr(PLATFORM_REVENUE, 5149),
        dr(PAYEE_PAYABLE_AVAILABLE, 14851, DOC),
        cr(PROVIDER_BALANCE, 20000, PA),
    )
    draft = only(drafts)
    assert (draft.status, draft.idempotency_key) == (EntryStatus.PENDING, f"refund:{FACT}:pending")
    assert dict(draft.metadata) == {"refund_id": str(FACT), "payment_id": str(UUID(int=51))}


def test_r8_internal_partial_refunds_never_drift() -> None:
    first = build_r8_internal(CTX, refund(amount=brl(10000)), payee_balances(DOC, available=14851))
    # allocate(10000, [14851, 5149]) = [7425.5, 2574.5] -> tie -> lower index: [7426, 2574]
    assert legs(first) == expect(
        dr(PLATFORM_REVENUE, 2574),
        dr(PAYEE_PAYABLE_AVAILABLE, 7426, DOC),
        cr(PROVIDER_BALANCE, 10000, PA),
    )
    second = build_r8_internal(
        CTX,
        refund(amount=brl(10000), already_refunded=brl(10000)),
        payee_balances(DOC, available=7425),
    )
    # Cumulative allocate(20000) = [14851, 5149]; difference [7425, 2575]: totals match R3 exactly.
    assert legs(second) == expect(
        dr(PLATFORM_REVENUE, 2575),
        dr(PAYEE_PAYABLE_AVAILABLE, 7425, DOC),
        cr(PROVIDER_BALANCE, 10000, PA),
    )


def test_r8_internal_waterfall_available_pending_held_then_receivable() -> None:
    balances = payee_balances(DOC, available=3000, pending=2000, held=1000)
    drafts = build_r8_internal(CTX, refund(amount=brl(10000), settled=False), balances)
    assert legs(drafts) == expect(
        dr(PLATFORM_REVENUE, 2574),
        dr(PAYEE_PAYABLE_AVAILABLE, 3000, DOC),
        dr(PAYEE_PAYABLE_PENDING, 2000, DOC),
        dr(PAYEE_PAYABLE_HELD, 1000, DOC),
        dr(PAYEE_RECEIVABLE, 1426, DOC),
        cr(PROVIDER_RECEIVABLE, 10000, PA),
    )


def test_r8_internal_ignores_negative_available_and_missing_accounts() -> None:
    overdrawn = {PAYEE_PAYABLE_AVAILABLE.code(DOC): credit_balance(100, pending_dr=500)}
    drafts = build_r8_internal(CTX, refund(amount=brl(10000)), overdrawn)
    assert dr(PAYEE_RECEIVABLE, 7426, DOC) in legs(drafts)


def test_r8_internal_negative_share_difference_is_credited_back() -> None:
    # Parts [1, 3, 3]: allocate(3) = [1, 1, 1], allocate(4) = [0, 2, 2] (largest remainder).
    parts = SplitAllocation((PayeeAmount(DOC, brl(1)), PayeeAmount(NURSE, brl(3))), brl(3))
    request = refund(amount=brl(1), gross=brl(7), already_refunded=brl(3), original=parts)
    drafts = build_r8_internal(CTX, request, payee_balances(NURSE, available=5))
    assert legs(drafts) == expect(
        cr(PAYEE_PAYABLE_AVAILABLE, 1, DOC),
        dr(PAYEE_PAYABLE_AVAILABLE, 1, NURSE),
        dr(PLATFORM_REVENUE, 1),
        cr(PROVIDER_BALANCE, 1, PA),
    )


def test_r8_no_split_refund_is_platform_only() -> None:
    request = refund(amount=brl(5000), original=SplitAllocation((), brl(20000)))
    assert legs(build_r8_internal(CTX, request, {})) == expect(
        dr(PLATFORM_REVENUE, 5000), cr(PROVIDER_BALANCE, 5000, PA)
    )


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"amount": brl(15000), "already_refunded": brl(5001)}, "cannot exceed"),
        ({"amount": brl(0)}, "amount must be > 0"),
        ({"already_refunded": brl(-1)}, ">= 0"),
        ({"original": SplitAllocation((), brl(0))}, "more than zero"),
        ({"gross": Money(20000, USD)}, "one currency"),
    ],
)
def test_r8_request_validation(overrides: dict[str, Any], error: str) -> None:
    with pytest.raises(ValidationFailed, match=error):
        refund(**overrides)


def test_r8_settled_must_be_bool() -> None:
    with pytest.raises(TypeError):
        refund(settled=1)


def provider_refund(*splits: RefundedSplit, amount: int = 10000) -> ProviderSplitRefundRequested:
    return ProviderSplitRefundRequested(
        refund_id=FACT,
        payment_id=UUID(int=51),
        provider_account_id=PA,
        amount=brl(amount),
        gross=brl(20000),
        already_refunded=brl(0),
        settled=True,
        refunded_splits=splits,
    )


def test_r8_provider_split_only_platform_part_and_not_done_splits_leave_our_account() -> None:
    request = provider_refund(
        RefundedSplit(DOC, brl(6000), done=True), RefundedSplit(NURSE, brl(1000), done=False)
    )
    assert legs(build_r8_provider_split(CTX, request)) == expect(
        dr(PLATFORM_REVENUE, 3000),
        dr(PAYEE_RECEIVABLE, 1000, NURSE),
        cr(PROVIDER_BALANCE, 4000, PA),
    )


@pytest.mark.parametrize(
    "splits",
    [
        (RefundedSplit(DOC, brl(10001), done=True),),
        (RefundedSplit(DOC, brl(1), done=True), RefundedSplit(DOC, brl(1), done=False)),
        (RefundedSplit(DOC, brl(0), done=True),),
    ],
)
def test_r8_provider_split_validation(splits: tuple[RefundedSplit, ...]) -> None:
    with pytest.raises(ValidationFailed):
        provider_refund(*splits)


def test_r8_provider_split_done_must_be_bool() -> None:
    with pytest.raises(TypeError):
        provider_refund(RefundedSplit(DOC, brl(1), done="yes"))  # pyright: ignore[reportArgumentType]


def test_r8_top_up_refund_uses_credits_then_receivable() -> None:
    request = TopUpRefundRequested(
        refund_id=FACT,
        payment_id=UUID(int=51),
        provider_account_id=PA,
        amount=brl(5000),
        gross=brl(10000),
        already_refunded=brl(0),
        settled=True,
        customer_id=C,
    )
    balances = {CUSTOMER_CREDITS.code(C): credit_balance(3000)}
    assert legs(build_r8_top_up(CTX, request, balances)) == expect(
        dr(CUSTOMER_CREDITS, 3000, C),
        dr(CUSTOMER_RECEIVABLE, 2000, C),
        cr(PROVIDER_BALANCE, 5000, PA),
    )
    assert legs(build_r8_top_up(CTX, request, {})) == expect(
        dr(CUSTOMER_RECEIVABLE, 5000, C), cr(PROVIDER_BALANCE, 5000, PA)
    )
    assert balance_accounts(request) == (CUSTOMER_CREDITS.spec(C), CUSTOMER_RECEIVABLE.spec(C))


def test_r8_unapplied_cash_refund() -> None:
    request = UnappliedCashRefundRequested(
        refund_id=FACT,
        payment_id=UUID(int=51),
        provider_account_id=PA,
        amount=brl(5000),
        gross=brl(5000),
        already_refunded=brl(0),
        settled=False,
        customer_id=C,
    )
    drafts = build_r8_unapplied(CTX, request)
    assert legs(drafts) == expect(
        dr(CUSTOMER_UNAPPLIED_CASH, 5000, C), cr(PROVIDER_RECEIVABLE, 5000, PA)
    )
    assert only(drafts).status is EntryStatus.PENDING


def test_balance_accounts_of_internal_refund() -> None:
    accounts = balance_accounts(refund())
    assert accounts == tuple(
        t.spec(DOC)
        for t in (
            PAYEE_PAYABLE_AVAILABLE,
            PAYEE_PAYABLE_PENDING,
            PAYEE_PAYABLE_HELD,
            PAYEE_RECEIVABLE,
        )
    )


# --- R9 -------------------------------------------------------------------------------------------


def test_r9_opened_holds_provider_funds_and_freezes_payee_share() -> None:
    opened = DisputeOpened(FACT, PA, brl(10000), GOLDEN_PARTS)
    assert opened.shares().payees == (PayeeAmount(DOC, brl(7426)),)
    drafts = build_r9_opened(CTX, opened, payee_balances(DOC, available=5000, pending=10000))
    assert legs(drafts) == expect(
        dr(PROVIDER_BALANCE_HELD, 10000, PA),
        cr(PROVIDER_BALANCE, 10000, PA),
        dr(PAYEE_PAYABLE_AVAILABLE, 5000, DOC),
        dr(PAYEE_PAYABLE_PENDING, 2426, DOC),
        cr(PAYEE_PAYABLE_HELD, 7426, DOC),
    )
    assert only(drafts).idempotency_key == f"dispute:{FACT}:opened"
    assert balance_accounts(opened) == tuple(
        t.spec(DOC) for t in (PAYEE_PAYABLE_AVAILABLE, PAYEE_PAYABLE_PENDING, PAYEE_PAYABLE_HELD)
    )


def test_r9_opened_with_payee_paid_out_freezes_nothing() -> None:
    drafts = build_r9_opened(CTX, DisputeOpened(FACT, PA, brl(10000), GOLDEN_PARTS), {})
    assert legs(drafts) == expect(
        dr(PROVIDER_BALANCE_HELD, 10000, PA), cr(PROVIDER_BALANCE, 10000, PA)
    )


def test_r9_lost_recovers_frozen_and_books_receivable_for_the_rest() -> None:
    lost = DisputeLost(FACT, PA, brl(10000), GOLDEN_PARTS, brl(1500))
    drafts = build_r9_lost(CTX, lost, {DOC: brl(7000)})
    assert legs(drafts) == expect(
        dr(PLATFORM_REVENUE, 2574),
        dr(PAYEE_PAYABLE_HELD, 7000, DOC),
        dr(PAYEE_RECEIVABLE, 426, DOC),
        cr(PROVIDER_BALANCE_HELD, 10000, PA),
        dr(PROVIDER_FEES, 1500),
        cr(PROVIDER_BALANCE, 1500, PA),
    )
    assert only(drafts).idempotency_key == f"dispute:{FACT}:lost"


@pytest.mark.parametrize("frozen", [{NURSE: brl(1)}, {DOC: brl(7427)}, {DOC: brl(-1)}])
def test_r9_lost_rejects_inconsistent_frozen_amounts(frozen: dict[PayeeId, Money]) -> None:
    lost = DisputeLost(FACT, PA, brl(10000), GOLDEN_PARTS, brl(0))
    with pytest.raises(ValidationFailed):
        build_r9_lost(CTX, lost, frozen)


def test_r9_validation() -> None:
    zero = SplitAllocation((), brl(0))
    with pytest.raises(ValidationFailed):
        DisputeOpened(FACT, PA, brl(0), GOLDEN_PARTS)
    with pytest.raises(ValidationFailed):
        DisputeOpened(FACT, PA, brl(1), zero)
    with pytest.raises(ValidationFailed):
        DisputeLost(FACT, PA, brl(1), GOLDEN_PARTS, brl(-1))
    with pytest.raises(ValidationFailed):
        DisputeLost(FACT, PA, brl(1), zero, brl(0))


# --- R10 / R12 / R13 ------------------------------------------------------------------------------


def test_r10_request_is_pending_and_references_the_approval() -> None:
    drafts = build_r10_request(CTX, PayoutRequested(FACT, DOC, PA, brl(5000), APPROVAL))
    assert legs(drafts) == expect(
        dr(PAYEE_PAYABLE_AVAILABLE, 5000, DOC), cr(PROVIDER_BALANCE, 5000, PA)
    )
    draft = only(drafts)
    assert draft.status is EntryStatus.PENDING
    assert dict(draft.metadata) == {"payout_id": str(FACT), "approval_id": str(UUID(int=60))}


def test_r10_fee_and_zero_fee() -> None:
    fee = build_r10_fee(CTX, PayoutPaid(FACT, PA, brl(300)))
    assert legs(fee) == expect(dr(PROVIDER_FEES, 300), cr(PROVIDER_BALANCE, 300, PA))
    assert only(fee).idempotency_key == f"payout:{FACT}:fee"
    assert build_r10_fee(CTX, PayoutPaid(FACT, PA, brl(0))) == ()


def test_approval_is_mandatory_for_payout_adjustment_and_write_off() -> None:
    missing: Any = None
    with pytest.raises(ValidationFailed, match="Approval"):
        PayoutRequested(FACT, DOC, PA, brl(1), missing)
    with pytest.raises(ValidationFailed, match="Approval"):
        ReconciliationAdjustment(FACT, PA, brl(1), "statement_break", missing)
    with pytest.raises(ValidationFailed, match="Approval"):
        WriteOff(FACT, brl(1), "uncollectible", missing, payee_id=DOC)


def test_r12_signed_adjustment_against_opening_balance() -> None:
    more = build_r12(CTX, ReconciliationAdjustment(FACT, PA, brl(700), "statement_break", APPROVAL))
    assert legs(more) == expect(dr(PROVIDER_BALANCE, 700, PA), cr(OPENING_BALANCE, 700))
    assert dict(only(more).metadata) == {
        "adjustment_id": str(FACT),
        "reason": "statement_break",
        "approval_id": str(UUID(int=60)),
    }
    assert only(more).idempotency_key == f"reconciliation:{FACT}:adjustment"
    less = build_r12(
        CTX, ReconciliationAdjustment(FACT, PA, brl(-700), "statement_break", APPROVAL)
    )
    assert legs(less) == expect(cr(PROVIDER_BALANCE, 700, PA), dr(OPENING_BALANCE, 700))


@pytest.mark.parametrize("reason", ["", "Paid to John Doe", "a" * 65, "-leading", 7])
def test_reason_must_be_a_code_not_free_text(reason: Any) -> None:
    with pytest.raises(ValidationFailed, match="reason"):
        ReconciliationAdjustment(FACT, PA, brl(1), reason, APPROVAL)


def test_r12_rejects_zero() -> None:
    with pytest.raises(ValidationFailed, match="zero"):
        ReconciliationAdjustment(FACT, PA, brl(0), "statement_break", APPROVAL)


def test_r13_write_off_payee_or_customer() -> None:
    payee = build_r13(CTX, WriteOff(FACT, brl(426), "uncollectible", APPROVAL, payee_id=DOC))
    assert legs(payee) == expect(dr(CHARGEBACK_LOSSES, 426), cr(PAYEE_RECEIVABLE, 426, DOC))
    assert only(payee).idempotency_key == f"write_off:{FACT}"
    customer = build_r13(CTX, WriteOff(FACT, brl(426), "uncollectible", APPROVAL, customer_id=C))
    assert legs(customer) == expect(dr(CHARGEBACK_LOSSES, 426), cr(CUSTOMER_RECEIVABLE, 426, C))
    with pytest.raises(ValidationFailed, match="exactly one"):
        WriteOff(FACT, brl(1), "uncollectible", APPROVAL)
    with pytest.raises(ValidationFailed, match="exactly one"):
        WriteOff(FACT, brl(1), "uncollectible", APPROVAL, payee_id=DOC, customer_id=C)


def test_amounts_must_be_money() -> None:
    with pytest.raises(TypeError):
        ProviderSplitBlocked(FACT, PA, 700)  # pyright: ignore[reportArgumentType]
