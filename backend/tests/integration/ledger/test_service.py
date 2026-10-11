"""LedgerService on Postgres as ``fh_app``: idempotency, two-phase entries, reversals, limits."""

from datetime import timedelta
from uuid import UUID

import pytest

from financial_hub.modules.ledger.application import (
    AccountSpec,
    AccountType,
    AggregateLock,
    Balance,
    Direction,
    EntryDraft,
    EntryStatus,
    JournalEntry,
    LedgerService,
)
from financial_hub.modules.ledger.domain import metadata_to_json
from financial_hub.shared.errors import (
    Conflict,
    IdempotencyKeyReused,
    InsufficientFunds,
    NotFound,
    ValidationFailed,
)
from financial_hub.shared.ids import new_uuid
from financial_hub.shared.money import BRL, USD
from tests.integration.ledger.support import (
    AT,
    CASH,
    FEES,
    PAYABLE,
    REVENUE,
    WALLET,
    Ledger,
    leg,
    transfer,
)


def columns(balance: Balance) -> tuple[int, int, int, int]:
    return balance.posted_dr, balance.posted_cr, balance.pending_dr, balance.pending_cr


async def fund_wallet(ledger: Ledger, project_id: UUID, units: int) -> None:
    await ledger.post(transfer(project_id, f"fund:{new_uuid()}", CASH, WALLET, units))


# --- posting ----------------------------------------------------------------------------------


async def test_post_entry_moves_balances(ledger: Ledger, project_id: UUID) -> None:
    (entry,) = await ledger.post(transfer(project_id, "sale-1", CASH, REVENUE, 1_000))

    assert entry.status is EntryStatus.POSTED
    assert entry.project_id == project_id
    assert entry.idempotency_key == "sale-1"
    assert entry.effective_at == AT
    assert sorted((p.direction, p.amount.minor_units) for p in entry.postings) == [
        (Direction.CREDIT, 1_000),
        (Direction.DEBIT, 1_000),
    ]
    assert columns(await ledger.balance(project_id, CASH)) == (1_000, 0, 0, 0)
    assert columns(await ledger.balance(project_id, REVENUE)) == (0, 1_000, 0, 0)
    assert (await ledger.balance(project_id, REVENUE)).posted.minor_units == 1_000
    await ledger.assert_consistent(project_id)


async def test_batch_returns_one_entry_per_input_in_order(ledger: Ledger, project_id: UUID) -> None:
    first = transfer(project_id, "b", CASH, REVENUE, 10)
    second = transfer(project_id, "a", FEES, CASH, 3)
    entries = await ledger.post(first, second, first)

    assert [e.idempotency_key for e in entries] == ["b", "a", "b"]
    assert entries[0] == entries[2]
    assert columns(await ledger.balance(project_id, CASH)) == (10, 3, 0, 0)
    await ledger.assert_consistent(project_id)


async def test_empty_batch_is_a_no_op(ledger: Ledger) -> None:
    assert await ledger.post() == []


async def test_multi_currency_entry(ledger: Ledger, project_id: UUID) -> None:
    draft = EntryDraft.create(
        project_id=project_id,
        idempotency_key="fx",
        recipe="test",
        postings=[
            leg(CASH, Direction.DEBIT, 500),
            leg(REVENUE, Direction.CREDIT, 500),
            leg(CASH, Direction.DEBIT, 90, USD),
            leg(REVENUE, Direction.CREDIT, 90, USD),
        ],
        effective_at=AT,
    )
    assert draft is not None
    (entry,) = await ledger.post(draft)

    assert len(entry.postings) == 4
    assert columns(await ledger.balance(project_id, CASH, USD)) == (90, 0, 0, 0)
    assert columns(await ledger.balance(project_id, CASH, BRL)) == (500, 0, 0, 0)
    await ledger.assert_consistent(project_id, BRL, USD)


async def test_zero_legs_are_dropped(ledger: Ledger, project_id: UUID) -> None:
    draft = EntryDraft.create(
        project_id=project_id,
        idempotency_key="with-zero-fee",
        recipe="test",
        postings=[
            leg(CASH, Direction.DEBIT, 100),
            leg(FEES, Direction.DEBIT, 0),
            leg(REVENUE, Direction.CREDIT, 100),
        ],
        effective_at=AT,
    )
    assert draft is not None
    (entry,) = await ledger.post(draft)

    assert len(entry.postings) == 2
    found = await ledger.run(lambda s: s.find_account(project_id, FEES.code, BRL))
    assert found is None  # a dropped leg does not even provision its account


async def test_metadata_and_trace_ids_round_trip(ledger: Ledger, project_id: UUID) -> None:
    correlation, causation = new_uuid(), new_uuid()
    metadata = {"order": "o-1", "count": 3, "ok": True, "tags": ["a", None], "nested": {"k": "v"}}
    draft = EntryDraft.create(
        project_id=project_id,
        idempotency_key="meta",
        recipe="checkout.capture",
        postings=[leg(CASH, Direction.DEBIT, 1), leg(REVENUE, Direction.CREDIT, 1)],
        effective_at=AT + timedelta(days=1),
        correlation_id=correlation,
        causation_id=causation,
        metadata=metadata,
    )
    assert draft is not None
    (posted,) = await ledger.post(draft)
    stored = await ledger.run(lambda s: s.get_entry(project_id, posted.id))

    assert stored == posted
    assert metadata_to_json(stored.metadata) == metadata
    assert (stored.recipe, stored.correlation_id, stored.causation_id) == (
        "checkout.capture",
        correlation,
        causation,
    )
    assert stored.effective_at == AT + timedelta(days=1)
    assert stored.payload_hash == draft.payload_hash


# --- idempotency ------------------------------------------------------------------------------


async def test_replay_returns_the_stored_entry_and_changes_nothing(
    ledger: Ledger, project_id: UUID
) -> None:
    draft = transfer(project_id, "pay-1", CASH, REVENUE, 700)
    (first,) = await ledger.post(draft)
    redelivered = EntryDraft.create(
        project_id=project_id,
        idempotency_key="pay-1",
        recipe="test",
        postings=[leg(REVENUE, Direction.CREDIT, 700), leg(CASH, Direction.DEBIT, 700)],
        effective_at=AT,
        correlation_id=new_uuid(),
        metadata={"delivery": 2},
    )
    assert redelivered is not None
    (again,) = await ledger.post(draft)
    (third,) = await ledger.post(redelivered)

    assert first == again == third
    assert columns(await ledger.balance(project_id, CASH)) == (700, 0, 0, 0)
    await ledger.assert_consistent(project_id)


async def test_same_key_with_another_payload_is_rejected(ledger: Ledger, project_id: UUID) -> None:
    await ledger.post(transfer(project_id, "pay-1", CASH, REVENUE, 700))

    with pytest.raises(IdempotencyKeyReused):
        await ledger.post(transfer(project_id, "pay-1", CASH, REVENUE, 701))
    with pytest.raises(IdempotencyKeyReused):
        await ledger.post(
            transfer(project_id, "fresh", CASH, REVENUE, 1),
            transfer(project_id, "pay-1", CASH, PAYABLE, 700),
        )
    assert columns(await ledger.balance(project_id, CASH)) == (700, 0, 0, 0)
    assert await ledger.run(lambda s: s.find_account(project_id, PAYABLE.code, BRL)) is None


async def test_same_key_twice_in_one_batch_with_another_payload_is_rejected(
    ledger: Ledger, project_id: UUID
) -> None:
    with pytest.raises(IdempotencyKeyReused):
        await ledger.post(
            transfer(project_id, "dup", CASH, REVENUE, 1),
            transfer(project_id, "dup", CASH, REVENUE, 2),
        )
    assert await ledger.account_ids(project_id) == []


async def test_keys_are_scoped_per_project(ledger: Ledger, project_id: UUID) -> None:
    other = new_uuid()
    (mine,) = await ledger.post(transfer(project_id, "k", CASH, REVENUE, 5))
    (theirs,) = await ledger.post(transfer(other, "k", CASH, REVENUE, 6))

    assert mine.id != theirs.id
    with pytest.raises(NotFound):
        await ledger.run(lambda s: s.get_entry(other, mine.id))


# --- two-phase (pending) entries --------------------------------------------------------------


async def test_pending_then_post(ledger: Ledger, project_id: UUID) -> None:
    draft = transfer(project_id, "hold", CASH, REVENUE, 300, pending=True)
    pending = await ledger.run(lambda s: s.create_pending(draft))

    assert pending.status is EntryStatus.PENDING
    assert columns(await ledger.balance(project_id, CASH)) == (0, 0, 300, 0)
    await ledger.assert_consistent(project_id)

    posted = await ledger.run(lambda s: s.post_pending(project_id, pending.id))
    assert posted.status is EntryStatus.POSTED
    assert columns(await ledger.balance(project_id, CASH)) == (300, 0, 0, 0)

    again = await ledger.run(lambda s: s.post_pending(project_id, pending.id))
    assert again.status is EntryStatus.POSTED
    assert columns(await ledger.balance(project_id, CASH)) == (300, 0, 0, 0)
    with pytest.raises(Conflict):
        await ledger.run(lambda s: s.void_pending(project_id, pending.id))
    stored = await ledger.run(lambda s: s.get_entry(project_id, pending.id))
    assert stored.status is EntryStatus.POSTED
    await ledger.assert_consistent(project_id)


async def test_pending_then_void(ledger: Ledger, project_id: UUID) -> None:
    pending = await ledger.run(
        lambda s: s.create_pending(transfer(project_id, "hold", CASH, REVENUE, 300, pending=True))
    )
    voided = await ledger.run(lambda s: s.void_pending(project_id, pending.id))

    assert voided.status is EntryStatus.ARCHIVED
    assert columns(await ledger.balance(project_id, CASH)) == (0, 0, 0, 0)
    again = await ledger.run(lambda s: s.void_pending(project_id, pending.id))
    assert again.status is EntryStatus.ARCHIVED
    with pytest.raises(Conflict):
        await ledger.run(lambda s: s.post_pending(project_id, pending.id))
    with pytest.raises(Conflict):
        await ledger.run(lambda s: s.reverse(project_id, pending.id, "rev", effective_at=AT))
    await ledger.assert_consistent(project_id)


async def test_transitions_of_posted_entries_are_rejected(ledger: Ledger, project_id: UUID) -> None:
    (entry,) = await ledger.post(transfer(project_id, "posted", CASH, REVENUE, 1))

    assert (await ledger.run(lambda s: s.post_pending(project_id, entry.id))) == entry
    with pytest.raises(Conflict):
        await ledger.run(lambda s: s.void_pending(project_id, entry.id))


async def test_create_pending_needs_a_pending_draft(ledger: Ledger, project_id: UUID) -> None:
    with pytest.raises(ValidationFailed):
        await ledger.run(lambda s: s.create_pending(transfer(project_id, "x", CASH, REVENUE, 1)))


async def test_unknown_ids_are_not_found(ledger: Ledger, project_id: UUID) -> None:
    unknown = new_uuid()
    for action in (
        lambda s: s.post_pending(project_id, unknown),
        lambda s: s.void_pending(project_id, unknown),
        lambda s: s.reverse(project_id, unknown, "r", effective_at=AT),
        lambda s: s.get_entry(project_id, unknown),
        lambda s: s.get_account(project_id, unknown),
        lambda s: s.get_balance(project_id, unknown),
        lambda s: s.recompute_balance_from_postings(project_id, unknown),
    ):
        with pytest.raises(NotFound):
            await ledger.run(action)


# --- reversals --------------------------------------------------------------------------------


async def test_reversal_nets_to_zero(ledger: Ledger, project_id: UUID) -> None:
    draft = EntryDraft.create(
        project_id=project_id,
        idempotency_key="capture",
        recipe="test",
        postings=[
            leg(CASH, Direction.DEBIT, 1_000),
            leg(FEES, Direction.DEBIT, 30),
            leg(CASH, Direction.CREDIT, 30),
            leg(PAYABLE, Direction.CREDIT, 900),
            leg(REVENUE, Direction.CREDIT, 100),
        ],
        effective_at=AT,
    )
    assert draft is not None
    (original,) = await ledger.post(draft)
    reversal = await ledger.run(
        lambda s: s.reverse(project_id, original.id, "capture:reversal", effective_at=AT)
    )

    assert reversal.reverses_entry_id == original.id
    assert reversal.recipe == "reversal"
    assert sorted((p.account_id, p.direction.opposite(), p.amount) for p in reversal.postings) == (
        sorted((p.account_id, p.direction, p.amount) for p in original.postings)
    )
    for spec in (CASH, FEES, PAYABLE, REVENUE):
        balance = await ledger.balance(project_id, spec)
        assert balance.posted.is_zero(), spec
        assert balance.posted_dr == balance.posted_cr > 0
    await ledger.assert_consistent(project_id)

    replay = await ledger.run(
        lambda s: s.reverse(project_id, original.id, "capture:reversal", effective_at=AT)
    )
    assert replay == reversal
    with pytest.raises(Conflict):
        await ledger.run(lambda s: s.reverse(project_id, original.id, "again", effective_at=AT))
    with pytest.raises(IdempotencyKeyReused):
        await ledger.run(
            lambda s: s.reverse(
                project_id, original.id, "capture:reversal", effective_at=AT + timedelta(days=1)
            )
        )


async def test_an_entry_is_reversed_once_per_batch(ledger: Ledger, project_id: UUID) -> None:
    (original,) = await ledger.post(transfer(project_id, "o", CASH, REVENUE, 5))

    def mirror(key: str) -> EntryDraft:
        draft = EntryDraft.create(
            project_id=project_id,
            idempotency_key=key,
            recipe="reversal",
            postings=[leg(CASH, Direction.CREDIT, 5), leg(REVENUE, Direction.DEBIT, 5)],
            effective_at=AT,
            reverses_entry_id=original.id,
        )
        assert draft is not None
        return draft

    with pytest.raises(Conflict):
        await ledger.post(mirror("r1"), mirror("r2"))
    assert columns(await ledger.balance(project_id, CASH)) == (5, 0, 0, 0)


async def test_a_pending_entry_cannot_be_reversed_through_post_entries(
    ledger: Ledger, project_id: UUID
) -> None:
    pending = await ledger.run(
        lambda s: s.create_pending(transfer(project_id, "hold", CASH, REVENUE, 5, pending=True))
    )
    draft = EntryDraft.create(
        project_id=project_id,
        idempotency_key="rev",
        recipe="reversal",
        postings=[leg(CASH, Direction.CREDIT, 5), leg(REVENUE, Direction.DEBIT, 5)],
        effective_at=AT,
        reverses_entry_id=pending.id,
    )
    assert draft is not None
    with pytest.raises(Conflict):
        await ledger.post(draft)
    assert columns(await ledger.balance(project_id, CASH)) == (0, 0, 5, 0)


async def test_reversal_can_overdraw_and_is_refused(ledger: Ledger, project_id: UUID) -> None:
    (funding,) = await ledger.post(transfer(project_id, "fund", CASH, WALLET, 100))
    await ledger.post(transfer(project_id, "spend", WALLET, REVENUE, 60))

    with pytest.raises(InsufficientFunds):
        await ledger.run(lambda s: s.reverse(project_id, funding.id, "unfund", effective_at=AT))
    assert (await ledger.balance(project_id, WALLET)).available.minor_units == 40


# --- no_overdraft -----------------------------------------------------------------------------


async def test_no_overdraft_rejects_and_leaves_no_trace(ledger: Ledger, project_id: UUID) -> None:
    await fund_wallet(ledger, project_id, 100)

    with pytest.raises(InsufficientFunds):
        await ledger.post(transfer(project_id, "too-much", WALLET, REVENUE, 101))
    with pytest.raises(InsufficientFunds):  # the whole batch counts, not each entry alone
        await ledger.post(
            transfer(project_id, "half-1", WALLET, REVENUE, 60),
            transfer(project_id, "half-2", WALLET, REVENUE, 60),
        )
    assert columns(await ledger.balance(project_id, WALLET)) == (0, 100, 0, 0)

    await ledger.post(transfer(project_id, "exact", WALLET, REVENUE, 100))
    assert (await ledger.balance(project_id, WALLET)).available.is_zero()
    await ledger.assert_consistent(project_id)


async def test_pending_debits_reduce_available(ledger: Ledger, project_id: UUID) -> None:
    await fund_wallet(ledger, project_id, 100)
    hold = await ledger.run(
        lambda s: s.create_pending(transfer(project_id, "hold", WALLET, PAYABLE, 60, pending=True))
    )
    wallet = await ledger.balance(project_id, WALLET)
    assert (wallet.posted.minor_units, wallet.available.minor_units) == (100, 40)

    with pytest.raises(InsufficientFunds):
        await ledger.post(transfer(project_id, "spend-50", WALLET, REVENUE, 50))
    await ledger.post(transfer(project_id, "spend-40", WALLET, REVENUE, 40))
    # Posting the hold moves it from pending to posted: available is unchanged (still 0).
    await ledger.run(lambda s: s.post_pending(project_id, hold.id))
    wallet = await ledger.balance(project_id, WALLET)
    assert (wallet.posted.minor_units, wallet.available.minor_units) == (0, 0)
    await ledger.assert_consistent(project_id)


async def test_voiding_a_hold_releases_available(ledger: Ledger, project_id: UUID) -> None:
    await fund_wallet(ledger, project_id, 100)
    hold = await ledger.run(
        lambda s: s.create_pending(transfer(project_id, "hold", WALLET, PAYABLE, 100, pending=True))
    )
    with pytest.raises(InsufficientFunds):
        await ledger.post(transfer(project_id, "spend", WALLET, REVENUE, 1))
    await ledger.run(lambda s: s.void_pending(project_id, hold.id))
    assert (await ledger.balance(project_id, WALLET)).available.minor_units == 100


async def test_pending_credit_is_not_available_yet(ledger: Ledger, project_id: UUID) -> None:
    await ledger.run(
        lambda s: s.create_pending(transfer(project_id, "topup", CASH, WALLET, 100, pending=True))
    )
    with pytest.raises(InsufficientFunds):
        await ledger.post(transfer(project_id, "spend", WALLET, REVENUE, 1))


# --- accounts and validation ------------------------------------------------------------------


async def test_account_definitions_must_match(ledger: Ledger, project_id: UUID) -> None:
    await ledger.post(transfer(project_id, "a", CASH, REVENUE, 1))
    as_liability = AccountSpec(CASH.code, AccountType.LIABILITY)

    with pytest.raises(Conflict):
        await ledger.post(transfer(project_id, "b", as_liability, REVENUE, 1))
    with pytest.raises(ValidationFailed):
        await ledger.post(
            transfer(project_id, "c", CASH, REVENUE, 1),
            transfer(project_id, "d", as_liability, PAYABLE, 1),
        )
    with pytest.raises(Conflict):
        await ledger.run(lambda s: s.provision_accounts(project_id, [(as_liability, BRL)]))


async def test_provision_accounts(ledger: Ledger, project_id: UUID) -> None:
    accounts = await ledger.run(
        lambda s: s.provision_accounts(project_id, [(WALLET, BRL), (WALLET, USD), (CASH, BRL)])
    )
    again = await ledger.run(lambda s: s.provision_accounts(project_id, [(WALLET, USD)]))

    wallet_usd = accounts[(WALLET.code, "USD")]
    assert again[(WALLET.code, "USD")] == wallet_usd
    assert wallet_usd.spec == WALLET
    assert wallet_usd.no_overdraft
    assert columns(wallet_usd.balance) == (0, 0, 0, 0)
    assert await ledger.run(lambda s: s.get_account(project_id, wallet_usd.id)) == wallet_usd
    assert await ledger.run(lambda s: s.find_account(project_id, WALLET.code, USD)) == wallet_usd
    with pytest.raises(NotFound):  # accounts are scoped by project
        await ledger.run(lambda s: s.get_account(new_uuid(), wallet_usd.id))


async def test_one_write_per_project_and_unit_of_work(ledger: Ledger, project_id: UUID) -> None:
    with pytest.raises(ValidationFailed):
        await ledger.post(
            transfer(project_id, "a", CASH, REVENUE, 1),
            transfer(new_uuid(), "b", CASH, REVENUE, 1),
        )

    async def two_writes(service: LedgerService) -> None:
        await service.post_entries([transfer(project_id, "first", CASH, REVENUE, 1)])
        await service.post_entries([transfer(project_id, "second", CASH, REVENUE, 1)])

    with pytest.raises(RuntimeError, match="already locked"):
        await ledger.run(two_writes)
    assert await ledger.account_ids(project_id) == []  # the unit of work rolled back


async def test_aggregate_lock_runs_first_in_every_write(ledger: Ledger, project_id: UUID) -> None:
    calls: list[str] = []

    def lock(name: str) -> AggregateLock:
        async def aggregate_lock() -> None:
            calls.append(name)

        return aggregate_lock

    (entry,) = await ledger.run(
        lambda s: s.post_entries(
            [transfer(project_id, "p", CASH, REVENUE, 2, pending=True)], aggregate_lock=lock("post")
        )
    )
    await ledger.run(lambda s: s.post_pending(project_id, entry.id, aggregate_lock=lock("commit")))
    await ledger.run(
        lambda s: s.reverse(project_id, entry.id, "r", effective_at=AT, aggregate_lock=lock("rev"))
    )
    assert calls == ["post", "commit", "rev"]


async def test_returned_entries_are_domain_objects(ledger: Ledger, project_id: UUID) -> None:
    (entry,) = await ledger.post(transfer(project_id, "t", CASH, REVENUE, 9))
    assert isinstance(entry, JournalEntry)
    cash = await ledger.run(lambda s: s.find_account(project_id, CASH.code, BRL))
    assert cash is not None
    assert {p.account_id for p in entry.postings} >= {cash.id}
    assert cash.version == 1
