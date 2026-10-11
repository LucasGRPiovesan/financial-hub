"""Posting recipes R1-R13 against real Postgres as ``fh_app`` (FINANCIAL_MODEL s4, s5).

Each recipe is applied twice to prove idempotency (same facts -> same stored entries, no new rows);
full flows prove the clearing accounts return to zero and the books stay consistent.
"""

from collections.abc import Mapping
from uuid import UUID

import pytest
from sqlalchemy import text

from financial_hub.modules.ledger.application import (
    CHARGEBACK_LOSSES,
    CUSTOMER_CREDITS,
    CUSTOMER_RECEIVABLE,
    CUSTOMER_UNAPPLIED_CASH,
    OPENING_BALANCE,
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
    Account,
    AccountTemplate,
    Approval,
    CustomerId,
    DeferredEntry,
    DisputeLost,
    DisputeOpened,
    DisputeWon,
    EntryDraft,
    EntryStatus,
    FeeAdjusted,
    FeeBearer,
    InvoiceFinalized,
    InvoiceVoided,
    JournalEntry,
    LedgerService,
    PayeeAmount,
    PayeeId,
    PaymentCaptured,
    PaymentSettled,
    PayoutFailed,
    PayoutPaid,
    PayoutRequested,
    PayoutReturned,
    ProviderAccountId,
    ProviderSplitBlocked,
    ProviderSplitDone,
    ProviderSplitRefundRequested,
    ProviderSplitUnblocked,
    ReceivedInCash,
    ReceivedInCashRejected,
    RecipeContext,
    RecipeInput,
    ReconciliationAdjustment,
    RefundCompleted,
    RefundedSplit,
    RefundOutcome,
    RefundRequested,
    SplitAllocated,
    SplitAllocation,
    SplitRule,
    SplitShare,
    TopUpCaptured,
    TopUpRefundRequested,
    Transition,
    UnappliedCashReceived,
    UnappliedCashRefundRequested,
    WalletConsumed,
    WriteOff,
    apply_recipe,
    apply_recipes,
)
from financial_hub.shared.errors import Conflict, InsufficientFunds, NotFound, ValidationFailed
from financial_hub.shared.ids import new_uuid
from financial_hub.shared.money import BRL, Money
from tests.integration.ledger.support import AT, CASH, REVENUE, Ledger, transfer

C = CustomerId(UUID(int=1))
PA = ProviderAccountId(UUID(int=2))
DOC = PayeeId(UUID(int=3))
INVOICE = UUID(int=10)
PAYMENT = UUID(int=11)
REFUND = UUID(int=12)
DISPUTE = UUID(int=13)
PAYOUT = UUID(int=14)
SPLIT = UUID(int=15)
APPROVAL = Approval(UUID(int=60), requested_by=UUID(int=61), approved_by=UUID(int=62))
DOCTOR_75 = SplitRule((SplitShare(DOC, 75),), 25)
GOLDEN_PARTS = SplitAllocation((PayeeAmount(DOC, Money(14851, BRL)),), Money(5149, BRL))


def brl(units: int) -> Money:
    return Money(units, BRL)


async def entry_count(ledger: Ledger, project_id: UUID) -> int:
    async with ledger.factory() as session:
        rows = await session.execute(
            text("SELECT count(*) FROM journal_entries WHERE project_id = :p"), {"p": project_id}
        )
        return rows.scalar_one()


async def apply(ledger: Ledger, project_id: UUID, *inputs: RecipeInput) -> tuple[JournalEntry, ...]:
    async with ledger.uow() as uow:
        result = await apply_recipes(uow, inputs, context=RecipeContext(project_id, AT))
    return result.entries


async def apply_twice(
    ledger: Ledger, project_id: UUID, *inputs: RecipeInput
) -> tuple[JournalEntry, ...]:
    """Apply, then replay the same facts: same entries back, no new rows (idempotency)."""
    first = await apply(ledger, project_id, *inputs)
    count = await entry_count(ledger, project_id)
    again = await apply(ledger, project_id, *inputs)
    assert [(e.id, e.status) for e in again] == [(e.id, e.status) for e in first]
    assert await entry_count(ledger, project_id) == count
    return first


async def posted(
    ledger: Ledger, project_id: UUID, template: AccountTemplate, owner: UUID | None = None
) -> int:
    return (await ledger.balance(project_id, template.spec(owner))).posted.minor_units


async def available(
    ledger: Ledger, project_id: UUID, template: AccountTemplate, owner: UUID | None = None
) -> int:
    return (await ledger.balance(project_id, template.spec(owner))).available.minor_units


async def assert_posted(
    ledger: Ledger, project_id: UUID, expected: Mapping[tuple[AccountTemplate, UUID | None], int]
) -> None:
    actual = {
        (template.name, owner): await posted(ledger, project_id, template, owner)
        for template, owner in expected
    }
    assert actual == {(t.name, owner): units for (t, owner), units in expected.items()}


async def golden(ledger: Ledger, project_id: UUID, *, internal: bool = True) -> None:
    """FINANCIAL_MODEL s5: R$200 PIX, fee 1.99, proportional, doctor 75 / platform 25."""
    (r1,) = await apply_twice(ledger, project_id, InvoiceFinalized(INVOICE, C, brl(20000), brl(0)))
    r2, r3 = await apply_twice(
        ledger,
        project_id,
        PaymentCaptured(PAYMENT, C, PA, brl(20000), brl(20000), brl(199)),
        SplitAllocated(PAYMENT, brl(20000), brl(19801), FeeBearer.PROPORTIONAL, DOCTOR_75),
    )
    shares = (PayeeAmount(DOC, brl(14851)),) if internal else ()
    (r4,) = await apply_twice(ledger, project_id, PaymentSettled(PAYMENT, PA, brl(19801), shares))
    assert [e.recipe for e in (r1, r2, r3, r4)] == ["R1", "R2", "R3", "R4"]
    assert all(e.status is EntryStatus.POSTED for e in (r1, r2, r3, r4))


# --- golden flow R1 -> R2 -> R3 -> R4 -------------------------------------------------------------


async def test_golden_health_check_example_s5(ledger: Ledger, project_id: UUID) -> None:
    await golden(ledger, project_id)
    await assert_posted(
        ledger,
        project_id,
        {
            (SALES_CLEARING, None): 0,
            (CUSTOMER_RECEIVABLE, C): 0,
            (PROVIDER_RECEIVABLE, PA): 0,
            (PROVIDER_BALANCE, PA): 19801,
            (PAYEE_PAYABLE_PENDING, DOC): 0,
            (PAYEE_PAYABLE_AVAILABLE, DOC): 14851,
            (PLATFORM_REVENUE, None): 5149,
            (PROVIDER_FEES, None): 199,
        },
    )
    # Platform net = revenue 5149 - provider fee 199 = 4950 (the platform part of N).
    net = await posted(ledger, project_id, PLATFORM_REVENUE) - await posted(
        ledger, project_id, PROVIDER_FEES
    )
    assert net == 4950
    await ledger.assert_consistent(project_id)


async def test_recipe_entries_carry_code_key_and_context(ledger: Ledger, project_id: UUID) -> None:
    correlation = new_uuid()
    async with ledger.uow() as uow:
        result = await apply_recipe(
            uow,
            InvoiceFinalized(INVOICE, C, brl(20000), brl(0)),
            context=RecipeContext(project_id, AT, correlation_id=correlation),
        )
    (entry,) = result.entries
    assert (entry.recipe, entry.idempotency_key) == ("R1", f"invoice:{INVOICE}:finalized")
    assert entry.correlation_id == correlation
    assert dict(entry.metadata) == {"invoice_id": str(INVOICE)}


async def test_aggregate_lock_runs_before_planning(ledger: Ledger, project_id: UUID) -> None:
    calls: list[str] = []

    async def lock() -> None:
        calls.append("locked")

    async with ledger.uow() as uow:
        await apply_recipes(
            uow,
            [InvoiceFinalized(INVOICE, C, brl(100), brl(0))],
            context=RecipeContext(project_id, AT),
            aggregate_lock=lock,
        )
    assert calls == ["locked"]


# --- R2a / R2b / R12 / RECEIVED_IN_CASH -----------------------------------------------------------


async def test_r2a_fee_adjustment(ledger: Ledger, project_id: UUID) -> None:
    await apply(
        ledger, project_id, PaymentCaptured(PAYMENT, C, PA, brl(20000), brl(20000), brl(199))
    )
    (entry,) = await apply_twice(
        ledger, project_id, FeeAdjusted(PAYMENT, PA, brl(20000), brl(199), brl(19750))
    )
    assert entry.recipe == "R2a"
    assert await posted(ledger, project_id, PROVIDER_FEES) == 250
    assert await posted(ledger, project_id, PROVIDER_RECEIVABLE, PA) == 19750
    unchanged = FeeAdjusted(PAYMENT, PA, brl(20000), brl(199), brl(19801))
    assert await apply(ledger, project_id, unchanged) == ()
    await ledger.assert_consistent(project_id)


async def test_r2b_unapplied_cash_and_its_refund(ledger: Ledger, project_id: UUID) -> None:
    (received,) = await apply_twice(
        ledger, project_id, UnappliedCashReceived(PAYMENT, C, PA, brl(5000), brl(0))
    )
    assert received.recipe == "R2b"
    assert await posted(ledger, project_id, CUSTOMER_UNAPPLIED_CASH, C) == 5000
    request = UnappliedCashRefundRequested(
        refund_id=REFUND,
        payment_id=PAYMENT,
        provider_account_id=PA,
        amount=brl(5000),
        gross=brl(5000),
        already_refunded=brl(0),
        settled=False,
        customer_id=C,
    )
    (pending,) = await apply_twice(ledger, project_id, request)
    assert pending.status is EntryStatus.PENDING
    await apply_twice(ledger, project_id, RefundCompleted(REFUND, RefundOutcome.DONE))
    await assert_posted(
        ledger, project_id, {(CUSTOMER_UNAPPLIED_CASH, C): 0, (PROVIDER_RECEIVABLE, PA): 0}
    )
    await ledger.assert_consistent(project_id)


async def test_r12_reconciliation_adjustment(ledger: Ledger, project_id: UUID) -> None:
    adjustment = ReconciliationAdjustment(new_uuid(), PA, brl(-700), "statement_break", APPROVAL)
    (entry,) = await apply_twice(ledger, project_id, adjustment)
    assert entry.recipe == "R12"
    assert entry.metadata["approval_id"] == str(APPROVAL.approval_id)
    assert entry.metadata["reason"] == "statement_break"
    await assert_posted(
        ledger, project_id, {(PROVIDER_BALANCE, PA): -700, (OPENING_BALANCE, None): -700}
    )
    await ledger.assert_consistent(project_id)


async def test_received_in_cash_is_rejected_and_writes_nothing(
    ledger: Ledger, project_id: UUID
) -> None:
    with pytest.raises(ReceivedInCashRejected):
        await apply(
            ledger,
            project_id,
            InvoiceFinalized(INVOICE, C, brl(100), brl(0)),
            ReceivedInCash(PAYMENT),
        )
    assert await entry_count(ledger, project_id) == 0


# --- R5 provider splits ---------------------------------------------------------------------------


async def test_r5_provider_split_done_blocked_unblocked_and_its_refund(
    ledger: Ledger, project_id: UUID
) -> None:
    await golden(ledger, project_id, internal=False)
    (done,) = await apply_twice(ledger, project_id, ProviderSplitDone(SPLIT, DOC, PA, brl(14851)))
    assert done.recipe == "R5"
    blocked_id = new_uuid()
    (blocked,) = await apply_twice(
        ledger, project_id, ProviderSplitBlocked(blocked_id, PA, brl(700))
    )
    await assert_posted(
        ledger,
        project_id,
        {
            (PAYEE_PAYABLE_PENDING, DOC): 0,
            (PROVIDER_BALANCE, PA): 4250,
            (PROVIDER_BALANCE_HELD, PA): 700,
        },
    )
    (unblocked,) = await apply_twice(ledger, project_id, ProviderSplitUnblocked(blocked_id))
    assert (unblocked.reverses_entry_id, unblocked.recipe) == (blocked.id, "R5")
    await assert_posted(
        ledger, project_id, {(PROVIDER_BALANCE, PA): 4950, (PROVIDER_BALANCE_HELD, PA): 0}
    )
    with pytest.raises(NotFound):
        await apply(ledger, project_id, ProviderSplitUnblocked(new_uuid()))

    # Full refund of a provider-split charge: the provider claws the executed split back itself.
    request = ProviderSplitRefundRequested(
        refund_id=REFUND,
        payment_id=PAYMENT,
        provider_account_id=PA,
        amount=brl(20000),
        gross=brl(20000),
        already_refunded=brl(0),
        settled=True,
        refunded_splits=(RefundedSplit(DOC, brl(14851), done=True),),
    )
    (pending,) = await apply_twice(ledger, project_id, request)
    assert pending.status is EntryStatus.PENDING
    await apply_twice(ledger, project_id, RefundCompleted(REFUND, RefundOutcome.DONE))
    await assert_posted(
        ledger, project_id, {(PLATFORM_REVENUE, None): 0, (PROVIDER_BALANCE, PA): -199}
    )
    await ledger.assert_consistent(project_id)


# --- R6 / R7 / R11 wallets ------------------------------------------------------------------------


async def test_r1_with_r7_wallet_then_r11_void(ledger: Ledger, project_id: UUID) -> None:
    (top_up,) = await apply_twice(
        ledger, project_id, TopUpCaptured(PAYMENT, C, PA, brl(5000), brl(5000), brl(0))
    )
    assert top_up.recipe == "R6"
    r1, r7 = await apply_twice(
        ledger,
        project_id,
        InvoiceFinalized(INVOICE, C, brl(20000), brl(5000)),
        WalletConsumed(INVOICE, C, brl(5000), DOCTOR_75),
    )
    assert (r1.recipe, r7.recipe) == ("R1", "R7")
    await assert_posted(
        ledger,
        project_id,
        {
            (CUSTOMER_CREDITS, C): 0,
            (CUSTOMER_RECEIVABLE, C): 15000,
            (SALES_CLEARING, None): 15000,
            (PAYEE_PAYABLE_AVAILABLE, DOC): 3750,
            (PLATFORM_REVENUE, None): 1250,
        },
    )
    voids = await apply_twice(ledger, project_id, InvoiceVoided(INVOICE))
    assert [(v.recipe, v.reverses_entry_id) for v in voids] == [("R11", r1.id), ("R11", r7.id)]
    await assert_posted(
        ledger,
        project_id,
        {
            (CUSTOMER_CREDITS, C): 5000,
            (CUSTOMER_RECEIVABLE, C): 0,
            (SALES_CLEARING, None): 0,
            (PAYEE_PAYABLE_AVAILABLE, DOC): 0,
            (PLATFORM_REVENUE, None): 0,
        },
    )
    assert await apply(ledger, project_id, InvoiceVoided(new_uuid())) == ()
    await ledger.assert_consistent(project_id)


async def test_wallets_are_segregated_per_project(ledger: Ledger, project_id: UUID) -> None:
    other_project = new_uuid()
    await apply(ledger, project_id, TopUpCaptured(PAYMENT, C, PA, brl(10000), brl(10000), brl(0)))
    consume = WalletConsumed(INVOICE, C, brl(5000), DOCTOR_75)
    # Same customer id, other project: its wallet is empty, the credits of project A are invisible.
    with pytest.raises(InsufficientFunds):
        await apply(ledger, other_project, consume)
    assert await ledger.account_ids(other_project) == []
    await apply(ledger, project_id, consume)
    assert await posted(ledger, project_id, CUSTOMER_CREDITS, C) == 5000
    with pytest.raises(InsufficientFunds):
        await apply(ledger, project_id, WalletConsumed(new_uuid(), C, brl(5001), DOCTOR_75))


async def test_r8_top_up_refund_after_partial_consumption(ledger: Ledger, project_id: UUID) -> None:
    await apply(ledger, project_id, TopUpCaptured(PAYMENT, C, PA, brl(10000), brl(10000), brl(0)))
    await apply(ledger, project_id, WalletConsumed(INVOICE, C, brl(4000), DOCTOR_75))
    request = TopUpRefundRequested(
        refund_id=REFUND,
        payment_id=PAYMENT,
        provider_account_id=PA,
        amount=brl(10000),
        gross=brl(10000),
        already_refunded=brl(0),
        settled=False,
        customer_id=C,
    )
    (pending,) = await apply_twice(ledger, project_id, request)
    assert pending.status is EntryStatus.PENDING
    assert await available(ledger, project_id, CUSTOMER_CREDITS, C) == 0
    await apply_twice(ledger, project_id, RefundCompleted(REFUND, RefundOutcome.DONE))
    await assert_posted(
        ledger,
        project_id,
        {(CUSTOMER_CREDITS, C): 0, (CUSTOMER_RECEIVABLE, C): 4000, (PROVIDER_RECEIVABLE, PA): 0},
    )
    await ledger.assert_consistent(project_id)


# --- R8 internal-split refunds (two-phase) --------------------------------------------------------


def internal_refund(refund_id: UUID, amount: int, already: int = 0) -> RefundRequested:
    return RefundRequested(
        refund_id=refund_id,
        payment_id=PAYMENT,
        provider_account_id=PA,
        amount=brl(amount),
        gross=brl(20000),
        already_refunded=brl(already),
        settled=True,
        original=GOLDEN_PARTS,
    )


async def test_r8_full_refund_done_returns_accounts_to_zero(
    ledger: Ledger, project_id: UUID
) -> None:
    await golden(ledger, project_id)
    (pending,) = await apply_twice(ledger, project_id, internal_refund(REFUND, 20000))
    assert (pending.status, pending.recipe) == (EntryStatus.PENDING, "R8")
    assert await available(ledger, project_id, PAYEE_PAYABLE_AVAILABLE, DOC) == 0
    assert await posted(ledger, project_id, PAYEE_PAYABLE_AVAILABLE, DOC) == 14851
    (done,) = await apply_twice(ledger, project_id, RefundCompleted(REFUND, RefundOutcome.DONE))
    assert (done.id, done.status) == (pending.id, EntryStatus.POSTED)
    await assert_posted(
        ledger,
        project_id,
        {
            (SALES_CLEARING, None): 0,
            (CUSTOMER_RECEIVABLE, C): 0,
            (PROVIDER_RECEIVABLE, PA): 0,
            (PAYEE_PAYABLE_AVAILABLE, DOC): 0,
            (PLATFORM_REVENUE, None): 0,
            # The non-refundable provider fee is the platform's loss.
            (PROVIDER_BALANCE, PA): -199,
            (PROVIDER_FEES, None): 199,
        },
    )
    with pytest.raises(Conflict):
        await apply(ledger, project_id, RefundCompleted(REFUND, RefundOutcome.DENIED))
    await ledger.assert_consistent(project_id)


async def test_r8_denied_refund_releases_the_hold(ledger: Ledger, project_id: UUID) -> None:
    await golden(ledger, project_id)
    await apply(ledger, project_id, internal_refund(REFUND, 5000))
    # allocate(5000, [14851, 5149]) = [3713, 1287]
    assert await available(ledger, project_id, PAYEE_PAYABLE_AVAILABLE, DOC) == 14851 - 3713
    (voided,) = await apply_twice(ledger, project_id, RefundCompleted(REFUND, RefundOutcome.DENIED))
    assert voided.status is EntryStatus.ARCHIVED
    assert await available(ledger, project_id, PAYEE_PAYABLE_AVAILABLE, DOC) == 14851
    assert await available(ledger, project_id, PROVIDER_BALANCE, PA) == 19801
    with pytest.raises(Conflict):
        await apply(ledger, project_id, RefundCompleted(REFUND, RefundOutcome.DONE))
    with pytest.raises(NotFound):
        await apply(ledger, project_id, RefundCompleted(new_uuid(), RefundOutcome.CANCELLED))
    await ledger.assert_consistent(project_id)


async def test_r8_partial_refunds_add_up_to_the_original_split(
    ledger: Ledger, project_id: UUID
) -> None:
    await golden(ledger, project_id)
    first, second = new_uuid(), new_uuid()
    await apply(ledger, project_id, internal_refund(first, 10000))
    await apply(ledger, project_id, internal_refund(second, 10000, already=10000))
    await apply(
        ledger,
        project_id,
        RefundCompleted(first, RefundOutcome.DONE),
        RefundCompleted(second, RefundOutcome.DONE),
    )
    await assert_posted(
        ledger, project_id, {(PAYEE_PAYABLE_AVAILABLE, DOC): 0, (PLATFORM_REVENUE, None): 0}
    )
    await ledger.assert_consistent(project_id)


# --- R10 payouts (two-phase) ----------------------------------------------------------------------


async def test_r10_payout_paid_failed_and_returned(ledger: Ledger, project_id: UUID) -> None:
    await golden(ledger, project_id)
    (pending,) = await apply_twice(
        ledger, project_id, PayoutRequested(PAYOUT, DOC, PA, brl(10000), APPROVAL)
    )
    assert (pending.status, pending.recipe) == (EntryStatus.PENDING, "R10")
    assert pending.metadata["approval_id"] == str(APPROVAL.approval_id)
    assert await available(ledger, project_id, PAYEE_PAYABLE_AVAILABLE, DOC) == 4851
    with pytest.raises(InsufficientFunds):
        await apply(ledger, project_id, PayoutRequested(new_uuid(), DOC, PA, brl(4852), APPROVAL))

    paid, fee = await apply_twice(ledger, project_id, PayoutPaid(PAYOUT, PA, brl(300)))
    assert (paid.id, paid.status) == (pending.id, EntryStatus.POSTED)
    assert (fee.recipe, fee.idempotency_key) == ("R10", f"payout:{PAYOUT}:fee")
    await assert_posted(
        ledger,
        project_id,
        {
            (PAYEE_PAYABLE_AVAILABLE, DOC): 4851,
            (PROVIDER_BALANCE, PA): 19801 - 10000 - 300,
            (PROVIDER_FEES, None): 199 + 300,
        },
    )

    failed_id = new_uuid()
    await apply(ledger, project_id, PayoutRequested(failed_id, DOC, PA, brl(4000), APPROVAL))
    assert await available(ledger, project_id, PAYEE_PAYABLE_AVAILABLE, DOC) == 851
    (failed,) = await apply_twice(ledger, project_id, PayoutFailed(failed_id))
    assert failed.status is EntryStatus.ARCHIVED
    assert await available(ledger, project_id, PAYEE_PAYABLE_AVAILABLE, DOC) == 4851
    with pytest.raises(Conflict):
        await apply(ledger, project_id, PayoutReturned(failed_id))

    (returned,) = await apply_twice(ledger, project_id, PayoutReturned(PAYOUT))
    assert (returned.reverses_entry_id, returned.recipe) == (pending.id, "R10")
    await assert_posted(
        ledger,
        project_id,
        {(PAYEE_PAYABLE_AVAILABLE, DOC): 14851, (PROVIDER_BALANCE, PA): 19801 - 300},
    )
    with pytest.raises(NotFound):
        await apply(ledger, project_id, PayoutPaid(new_uuid(), PA, brl(0)))
    await ledger.assert_consistent(project_id)


# --- R9 disputes, R13 write-off -------------------------------------------------------------------


async def test_r9_lost_then_r13_write_off_clears_payee_receivable(
    ledger: Ledger, project_id: UUID
) -> None:
    await golden(ledger, project_id)
    await apply(ledger, project_id, PayoutRequested(PAYOUT, DOC, PA, brl(10000), APPROVAL))
    await apply(ledger, project_id, PayoutPaid(PAYOUT, PA, brl(0)))
    (opened,) = await apply_twice(
        ledger, project_id, DisputeOpened(DISPUTE, PA, brl(20000), GOLDEN_PARTS)
    )
    assert opened.recipe == "R9"
    # Doctor's share 14851, only 4851 still with us: frozen from available.
    await assert_posted(
        ledger,
        project_id,
        {
            (PAYEE_PAYABLE_AVAILABLE, DOC): 0,
            (PAYEE_PAYABLE_HELD, DOC): 4851,
            (PROVIDER_BALANCE_HELD, PA): 20000,
        },
    )
    (lost,) = await apply_twice(
        ledger, project_id, DisputeLost(DISPUTE, PA, brl(20000), GOLDEN_PARTS, brl(1500))
    )
    assert lost.idempotency_key == f"dispute:{DISPUTE}:lost"
    await assert_posted(
        ledger,
        project_id,
        {
            (PAYEE_PAYABLE_HELD, DOC): 0,
            (PAYEE_RECEIVABLE, DOC): 10000,
            (PROVIDER_BALANCE_HELD, PA): 0,
            (PLATFORM_REVENUE, None): 0,
            (PROVIDER_FEES, None): 199 + 1500,
        },
    )
    with pytest.raises(Conflict):
        await apply(ledger, project_id, DisputeWon(DISPUTE))

    write_off = WriteOff(new_uuid(), brl(10000), "uncollectible", APPROVAL, payee_id=DOC)
    (written_off,) = await apply_twice(ledger, project_id, write_off)
    assert (written_off.recipe, written_off.metadata["approval_id"]) == (
        "R13",
        str(APPROVAL.approval_id),
    )
    await assert_posted(
        ledger, project_id, {(PAYEE_RECEIVABLE, DOC): 0, (CHARGEBACK_LOSSES, None): 10000}
    )
    await ledger.assert_consistent(project_id)


async def test_r9_won_releases_holds(ledger: Ledger, project_id: UUID) -> None:
    await golden(ledger, project_id)
    (opened,) = await apply(
        ledger, project_id, DisputeOpened(DISPUTE, PA, brl(10000), GOLDEN_PARTS)
    )
    # allocate(10000, [14851, 5149]) -> doctor 7426 frozen
    assert await available(ledger, project_id, PAYEE_PAYABLE_AVAILABLE, DOC) == 7425
    assert await posted(ledger, project_id, PAYEE_PAYABLE_HELD, DOC) == 7426
    (won,) = await apply_twice(ledger, project_id, DisputeWon(DISPUTE))
    assert (won.reverses_entry_id, won.recipe) == (opened.id, "R9")
    await assert_posted(
        ledger,
        project_id,
        {
            (PAYEE_PAYABLE_AVAILABLE, DOC): 14851,
            (PAYEE_PAYABLE_HELD, DOC): 0,
            (PROVIDER_BALANCE_HELD, PA): 0,
            (PROVIDER_BALANCE, PA): 19801,
        },
    )
    with pytest.raises(Conflict):
        await apply(ledger, project_id, DisputeLost(DISPUTE, PA, brl(10000), GOLDEN_PARTS, brl(0)))
    with pytest.raises(NotFound):
        await apply(ledger, project_id, DisputeWon(new_uuid()))
    await ledger.assert_consistent(project_id)


async def test_r13_customer_write_off(ledger: Ledger, project_id: UUID) -> None:
    await apply(ledger, project_id, InvoiceFinalized(INVOICE, C, brl(3000), brl(0)))
    write_off = WriteOff(new_uuid(), brl(3000), "uncollectible", APPROVAL, customer_id=C)
    await apply_twice(ledger, project_id, write_off)
    assert await posted(ledger, project_id, CUSTOMER_RECEIVABLE, C) == 0
    await ledger.assert_consistent(project_id)


# --- LedgerService.write: deferred entries and transitions ----------------------------------------


def nothing(_: Mapping[tuple[str, str], Account]) -> EntryDraft | None:
    return None


async def test_write_of_nothing(ledger: Ledger, project_id: UUID) -> None:
    assert await ledger.run(lambda service: service.write(project_id, [])) == []
    assert await ledger.run(lambda service: service.post_entries([])) == []
    deferred = DeferredEntry("k", ((CASH, BRL),), nothing)
    assert await ledger.run(lambda service: service.write(project_id, [deferred])) == [None]
    found = await ledger.run(lambda service: service.find_entry_by_key(project_id, "k"))
    assert found is None
    assert await apply(ledger, project_id) == ()


async def test_deferred_builder_sees_balances_after_earlier_items(
    ledger: Ledger, project_id: UUID
) -> None:
    def mirror(accounts: Mapping[tuple[str, str], Account]) -> EntryDraft:
        revenue = accounts[(REVENUE.code, BRL.code)].balance.posted.minor_units
        return transfer(project_id, "mirror", REVENUE, CASH, revenue)

    first = transfer(project_id, "first", CASH, REVENUE, 100)
    deferred = DeferredEntry("mirror", ((CASH, BRL), (REVENUE, BRL)), mirror)
    entry, built = await ledger.run(lambda service: service.write(project_id, [first, deferred]))
    assert entry is not None and built is not None
    assert built.postings[0].amount == brl(100)
    # Replay by key: the stored entry comes back, the builder (now seeing 0) is not called again.
    _, replayed = await ledger.run(lambda service: service.write(project_id, [first, deferred]))
    assert replayed is not None and replayed.id == built.id
    ids = [p.account_id for p in built.postings]
    accounts = await ledger.run(lambda service: service.get_accounts(project_id, ids))
    assert {a.spec.code for a in accounts.values()} == {CASH.code, REVENUE.code}


async def test_deferred_builder_must_respect_its_declaration(
    ledger: Ledger, project_id: UUID
) -> None:
    def undeclared(_: Mapping[tuple[str, str], Account]) -> EntryDraft:
        return transfer(project_id, "k", CASH, REVENUE, 1)

    def wrong_key(_: Mapping[tuple[str, str], Account]) -> EntryDraft:
        return transfer(project_id, "other", CASH, REVENUE, 1)

    with pytest.raises(RuntimeError, match="undeclared"):
        await ledger.run(
            lambda service: service.write(
                project_id, [DeferredEntry("k", ((CASH, BRL),), undeclared)]
            )
        )
    with pytest.raises(RuntimeError, match="declared project and key"):
        await ledger.run(
            lambda service: service.write(
                project_id, [DeferredEntry("k", ((CASH, BRL), (REVENUE, BRL)), wrong_key)]
            )
        )


async def test_write_validates_items_before_locking(ledger: Ledger, project_id: UUID) -> None:
    (pending,) = await ledger.post(transfer(project_id, "p", CASH, REVENUE, 5, pending=True))
    deferred = DeferredEntry("d", ((CASH, BRL),), nothing)
    invalid: list[list[EntryDraft | DeferredEntry | Transition]] = [
        [transfer(new_uuid(), "x", CASH, REVENUE, 1)],
        [transfer(project_id, "d", CASH, REVENUE, 1), deferred],
        [deferred, deferred],
        [Transition(pending.id, EntryStatus.POSTED), Transition(pending.id, EntryStatus.ARCHIVED)],
        [Transition(pending.id, EntryStatus.PENDING)],
    ]
    for items in invalid:
        with pytest.raises(ValidationFailed):
            await ledger.run(lambda service, items=items: service.write(project_id, items))
    with pytest.raises(NotFound):
        await ledger.run(
            lambda service: service.write(project_id, [Transition(new_uuid(), EntryStatus.POSTED)])
        )
    (posted_entry,) = await ledger.run(
        lambda service: service.write(project_id, [Transition(pending.id, EntryStatus.POSTED)])
    )
    assert posted_entry is not None and posted_entry.status is EntryStatus.POSTED
    await ledger.assert_consistent(project_id)


async def test_reverse_uses_build_reversal(ledger: Ledger, project_id: UUID) -> None:
    (entry,) = await ledger.post(transfer(project_id, "e", CASH, REVENUE, 9))

    async def reverse(service: LedgerService) -> JournalEntry:
        return await service.reverse(project_id, entry.id, "e:reversal", effective_at=AT)

    reversal = await ledger.run(reverse)
    assert reversal.reverses_entry_id == entry.id
    (pending,) = await ledger.post(transfer(project_id, "p", CASH, REVENUE, 1, pending=True))
    with pytest.raises(Conflict, match="only posted"):
        await ledger.run(
            lambda service: service.build_reversal(project_id, pending.id, "p:r", effective_at=AT)
        )
