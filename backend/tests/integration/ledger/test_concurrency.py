"""Concurrency of the ledger lock protocol (ADR-0003 s5) on separate connections, as ``fh_app``.

Every task runs its own unit of work on its own pooled connection; all tasks start together behind
a barrier and touch overlapping accounts with legs in random order. Any deadlock, serialization
error or lost update would surface as an exception or a wrong final balance.
"""

import asyncio
import random
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from uuid import UUID

import pytest
from sqlalchemy import text

from financial_hub.modules.ledger.application import (
    AccountSpec,
    AccountType,
    Direction,
    EntryDraft,
    EntryStatus,
    JournalEntry,
    PostingDraft,
)
from financial_hub.platform.db import create_session_factory
from financial_hub.shared.errors import Conflict, InsufficientFunds
from financial_hub.shared.ids import new_uuid
from financial_hub.shared.money import BRL, USD, Currency, Money
from tests.integration.ledger.support import (
    AT,
    CASH,
    FEES,
    PAYABLE,
    REVENUE,
    WALLET,
    Ledger,
    make_app_engine,
    transfer,
)

SEED = 20261010
TASKS = 25
CALLS_PER_TASK = 8
SHARED = (
    CASH,
    REVENUE,
    FEES,
    PAYABLE,
    AccountSpec("bank:clearing", AccountType.ASSET),
    AccountSpec("platform:equity", AccountType.EQUITY),
)


@pytest.fixture
async def busy_ledger() -> AsyncIterator[Ledger]:
    engine = make_app_engine(pool_size=TASKS + 5)
    try:
        yield Ledger(create_session_factory(engine))
    finally:
        await engine.dispose()


async def together[T](actions: list[Callable[[], Awaitable[T]]]) -> list[T | BaseException]:
    """Start every action at the same time; collect results and exceptions in order."""
    start = asyncio.Event()

    async def run(action: Callable[[], Awaitable[T]]) -> T:
        await start.wait()
        return await action()

    tasks = [asyncio.create_task(run(action)) for action in actions]
    await asyncio.sleep(0)
    start.set()
    return await asyncio.gather(*tasks, return_exceptions=True)


def random_entry(rng: random.Random, project_id: UUID, key: str) -> EntryDraft:
    """2-4 distinct shared accounts, random split, legs shuffled (random lock request order)."""
    currency: Currency = rng.choice((BRL, USD))
    accounts = rng.sample(SHARED, rng.randint(2, 4))
    debits = rng.randint(1, len(accounts) - 1)
    debit_amounts = [rng.randint(1, 1_000) for _ in range(debits)]
    total = sum(debit_amounts)
    credits = len(accounts) - debits
    total = max(total, credits)
    debit_amounts[0] += total - sum(debit_amounts)
    cuts = sorted(rng.sample(range(1, total), credits - 1)) if credits > 1 else []
    credit_amounts = [b - a for a, b in zip([0, *cuts], [*cuts, total], strict=True)]
    legs = [
        PostingDraft(spec, Direction.DEBIT, Money(units, currency))
        for spec, units in zip(accounts[:debits], debit_amounts, strict=True)
    ] + [
        PostingDraft(spec, Direction.CREDIT, Money(units, currency))
        for spec, units in zip(accounts[debits:], credit_amounts, strict=True)
    ]
    rng.shuffle(legs)
    draft = EntryDraft.create(
        project_id=project_id, idempotency_key=key, recipe="load", postings=legs, effective_at=AT
    )
    assert draft is not None
    return draft


async def test_concurrent_overlapping_writes_are_exact(busy_ledger: Ledger) -> None:
    rng = random.Random(SEED)  # noqa: S311 - reproducible test data, not security
    project_id = new_uuid()
    calls: list[list[EntryDraft]] = [
        [random_entry(rng, project_id, f"t{task}:c{call}:e{n}") for n in range(rng.randint(1, 3))]
        for task in range(TASKS)
        for call in range(CALLS_PER_TASK)
    ]
    expected: Counter[tuple[str, str, Direction]] = Counter()
    for drafts in calls:
        for draft in drafts:
            for p in draft.postings:
                expected[(p.account.code, p.amount.currency.code, p.direction)] += (
                    p.amount.minor_units
                )

    async def worker(task: int) -> int:
        posted = 0
        for drafts in calls[task * CALLS_PER_TASK : (task + 1) * CALLS_PER_TASK]:
            posted += len(await busy_ledger.post(*drafts))
        return posted

    results = await together([lambda task=task: worker(task) for task in range(TASKS)])

    errors = [r for r in results if isinstance(r, BaseException)]
    assert errors == []
    entries = sum(len(drafts) for drafts in calls)
    assert sum(r for r in results if isinstance(r, int)) == entries
    for spec in SHARED:
        for currency in (BRL, USD):
            balance = await busy_ledger.balance(project_id, spec, currency)
            assert balance.posted_dr == expected[(spec.code, currency.code, Direction.DEBIT)]
            assert balance.posted_cr == expected[(spec.code, currency.code, Direction.CREDIT)]
    await busy_ledger.assert_consistent(project_id, BRL, USD)
    async with busy_ledger.factory() as session:
        stored = await session.scalar(
            text("SELECT count(*) FROM journal_entries WHERE project_id = :p"), {"p": project_id}
        )
    assert stored == entries
    print(f"\n[concurrency] {TASKS} tasks x {CALLS_PER_TASK} calls: {entries} entries, 0 errors")


async def test_no_overdraft_admits_exactly_the_affordable_count(busy_ledger: Ledger) -> None:
    project_id = new_uuid()
    await busy_ledger.post(transfer(project_id, "fund", CASH, WALLET, 100))
    rng = random.Random(SEED)  # noqa: S311 - reproducible test data, not security

    def spend(n: int) -> Callable[[], Awaitable[list[JournalEntry]]]:
        # Also touches FEES/REVENUE in random leg order so lock requests interleave.
        legs = [
            PostingDraft(WALLET, Direction.DEBIT, Money(7, BRL)),
            PostingDraft(REVENUE, Direction.CREDIT, Money(5, BRL)),
            PostingDraft(FEES, Direction.DEBIT, Money(1, BRL)),
            PostingDraft(PAYABLE, Direction.CREDIT, Money(3, BRL)),
        ]
        rng.shuffle(legs)
        draft = EntryDraft.create(
            project_id=project_id,
            idempotency_key=f"spend-{n}",
            recipe="spend",
            postings=legs,
            effective_at=AT,
        )
        assert draft is not None
        return lambda: busy_ledger.post(draft)

    results = await together([spend(n) for n in range(30)])

    succeeded = [r for r in results if not isinstance(r, BaseException)]
    refused = [r for r in results if isinstance(r, InsufficientFunds)]
    others = [r for r in results if isinstance(r, BaseException) and r not in refused]
    assert others == []
    assert (len(succeeded), len(refused)) == (14, 16)
    wallet = await busy_ledger.balance(project_id, WALLET)
    assert (wallet.posted.minor_units, wallet.available.minor_units) == (2, 2)
    await busy_ledger.assert_consistent(project_id)
    print(f"\n[concurrency] 100 / 30 x 7: {len(succeeded)} ok, {len(refused)} refused")


async def test_racing_post_and_void_have_one_winner_per_entry(busy_ledger: Ledger) -> None:
    project_id = new_uuid()
    await busy_ledger.post(transfer(project_id, "fund", CASH, WALLET, 1_000))
    holds = await busy_ledger.post(
        *(
            transfer(project_id, f"hold-{n}", WALLET, PAYABLE, 10 + n, pending=True)
            for n in range(8)
        )
    )
    rng = random.Random(SEED)  # noqa: S311 - reproducible test data, not security
    actions: list[Callable[[], Awaitable[JournalEntry]]] = []
    for hold in holds:
        for _ in range(3):
            actions.append(
                lambda h=hold: busy_ledger.run(lambda s: s.post_pending(project_id, h.id))
            )
            actions.append(
                lambda h=hold: busy_ledger.run(lambda s: s.void_pending(project_id, h.id))
            )
    order = list(range(len(actions)))
    rng.shuffle(order)

    shuffled = await together([actions[i] for i in order])
    results: list[JournalEntry | BaseException] = [
        shuffled[order.index(i)] for i in range(len(order))
    ]

    assert [
        r for r in results if isinstance(r, BaseException) and not isinstance(r, Conflict)
    ] == []
    final = {
        hold.id: (await busy_ledger.run(lambda s, h=hold: s.get_entry(project_id, h.id))).status
        for hold in holds
    }
    for index, hold in enumerate(holds):
        outcomes = results[index * 6 : (index + 1) * 6]
        posts, voids = outcomes[0::2], outcomes[1::2]
        if final[hold.id] is EntryStatus.POSTED:
            assert all(
                isinstance(r, JournalEntry) and r.status is EntryStatus.POSTED for r in posts
            )
            assert all(isinstance(r, Conflict) for r in voids)
        else:
            assert final[hold.id] is EntryStatus.ARCHIVED
            assert all(
                isinstance(r, JournalEntry) and r.status is EntryStatus.ARCHIVED for r in voids
            )
            assert all(isinstance(r, Conflict) for r in posts)
    posted = sum(10 + n for n, hold in enumerate(holds) if final[hold.id] is EntryStatus.POSTED)
    wallet = await busy_ledger.balance(project_id, WALLET)
    assert (wallet.posted_dr, wallet.pending_dr) == (posted, 0)
    await busy_ledger.assert_consistent(project_id)


async def test_concurrent_replays_store_one_entry(busy_ledger: Ledger) -> None:
    project_id = new_uuid()
    draft = transfer(project_id, "same-key", CASH, REVENUE, 250)

    results = await together([lambda: busy_ledger.post(draft) for _ in range(TASKS)])

    assert [r for r in results if isinstance(r, BaseException)] == []
    ids = {entries[0].id for entries in results if isinstance(entries, list)}
    assert len(ids) == 1
    assert (await busy_ledger.balance(project_id, CASH)).posted_dr == 250
    await busy_ledger.assert_consistent(project_id)
