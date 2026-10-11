"""Stateful property test of the ledger on Postgres (``fh_app``), FINANCIAL_MODEL s2 invariants.

Hypothesis drives random sequences of transfers (posted or pending, BRL/USD), post/void of holds,
reversals and idempotent replays. A pure model built from the domain ``Balance`` predicts every
balance and every ``InsufficientFunds``. After **every** step: the trial balance is zero per
currency, each cached balance equals its recomputation from postings, ``no_overdraft`` accounts
have ``available >= 0`` and the database equals the model.
"""

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from uuid import UUID

import pytest
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)

from financial_hub.modules.ledger.application import (
    AccountSpec,
    Balance,
    Direction,
    EntryDraft,
    EntryStatus,
    JournalEntry,
    LedgerService,
)
from financial_hub.modules.ledger.domain import balance_deltas
from financial_hub.platform.db import create_session_factory
from financial_hub.shared.errors import InsufficientFunds
from financial_hub.shared.ids import new_uuid
from financial_hub.shared.money import BRL, USD, Currency, Money
from tests.integration.ledger.support import (
    AT,
    CASH,
    PAYABLE,
    REVENUE,
    WALLET,
    Ledger,
    leg,
    make_app_engine,
)

SPECS = (CASH, REVENUE, PAYABLE, WALLET)
CURRENCIES = (BRL, USD)

type Leg = tuple[UUID, Direction, Money]


@dataclass
class Stored:
    entry: JournalEntry
    draft: EntryDraft


class LedgerMachine(RuleBasedStateMachine):
    runner: asyncio.Runner
    ledger: Ledger

    def __init__(self) -> None:
        super().__init__()
        self.project_id = new_uuid()
        self.model: dict[UUID, Balance] = {}
        self.no_overdraft: set[UUID] = set()
        self.ids: dict[tuple[AccountSpec, Currency], UUID] = {}
        self.pending: list[Stored] = []
        self.reversible: list[Stored] = []
        self.history: list[Stored] = []
        self.keys = 0

    def run[T](self, action: Callable[[LedgerService], Awaitable[T]]) -> T:
        return self.runner.run(self.ledger.run(action))

    @initialize()
    def provision(self) -> None:
        pairs = [(spec, currency) for spec in SPECS for currency in CURRENCIES]
        accounts = self.run(lambda s: s.provision_accounts(self.project_id, pairs))
        for spec, currency in pairs:
            account = accounts[(spec.code, currency.code)]
            self.ids[(spec, currency)] = account.id
            self.model[account.id] = account.balance
            if spec.no_overdraft:
                self.no_overdraft.add(account.id)

    # --- prediction ---------------------------------------------------------------------------

    def predict(
        self, legs: Iterable[Leg], source: EntryStatus | None, target: EntryStatus
    ) -> dict[UUID, Balance] | None:
        """Balances after the transition, or ``None`` when a no_overdraft account would go < 0."""
        after = dict(self.model)
        for account_id, delta in balance_deltas(legs, source, target).items():
            after[account_id] = after[account_id].apply(delta)
        if any(after[a].available.is_negative() for a in self.no_overdraft):
            return None
        return after

    def legs_of(self, entry: JournalEntry) -> list[Leg]:
        return [(p.account_id, p.direction, p.amount) for p in entry.postings]

    def next_key(self) -> str:
        self.keys += 1
        return f"k{self.keys}"

    # --- rules --------------------------------------------------------------------------------

    @rule(
        debit=st.sampled_from(SPECS),
        credit=st.sampled_from(SPECS),
        units=st.integers(min_value=1, max_value=500),
        currency=st.sampled_from(CURRENCIES),
        pending=st.booleans(),
    )
    def transfer(
        self, debit: AccountSpec, credit: AccountSpec, units: int, currency: Currency, pending: bool
    ) -> None:
        if debit == credit:
            return
        draft = EntryDraft.create(
            project_id=self.project_id,
            idempotency_key=self.next_key(),
            recipe="property",
            postings=[
                leg(credit, Direction.CREDIT, units, currency),
                leg(debit, Direction.DEBIT, units, currency),
            ],
            effective_at=AT,
            pending=pending,
        )
        assert draft is not None
        legs = [(self.ids[(p.account, currency)], p.direction, p.amount) for p in draft.postings]
        after = self.predict(legs, None, draft.status)
        if after is None:
            with pytest.raises(InsufficientFunds):
                self.run(lambda s: s.post_entries([draft]))
            return
        (entry,) = self.run(lambda s: s.post_entries([draft]))
        assert entry.status is draft.status
        self.model = after
        stored = Stored(entry, draft)
        self.history.append(stored)
        (self.pending if pending else self.reversible).append(stored)

    @precondition(lambda self: self.pending)
    @rule(pick=st.integers(min_value=0), post=st.booleans())
    def settle_pending(self, pick: int, post: bool) -> None:
        stored = self.pending[pick % len(self.pending)]
        target = EntryStatus.POSTED if post else EntryStatus.ARCHIVED
        after = self.predict(self.legs_of(stored.entry), EntryStatus.PENDING, target)
        entry_id = stored.entry.id

        async def settle(service: LedgerService) -> JournalEntry:
            if post:
                return await service.post_pending(self.project_id, entry_id)
            return await service.void_pending(self.project_id, entry_id)

        if after is None:  # cannot happen today (settling never lowers available); kept generic
            with pytest.raises(InsufficientFunds):
                self.run(settle)
            return
        entry = self.run(settle)
        assert entry.status is target
        self.model = after
        self.pending.remove(stored)
        if post:
            self.reversible.append(Stored(entry, stored.draft))

    @precondition(lambda self: self.reversible)
    @rule(pick=st.integers(min_value=0))
    def reverse(self, pick: int) -> None:
        stored = self.reversible[pick % len(self.reversible)]
        flipped = [(a, d.opposite(), m) for a, d, m in self.legs_of(stored.entry)]
        after = self.predict(flipped, None, EntryStatus.POSTED)
        key = self.next_key()
        entry_id = stored.entry.id
        if after is None:
            with pytest.raises(InsufficientFunds):
                self.run(lambda s: s.reverse(self.project_id, entry_id, key, effective_at=AT))
            return
        reversal = self.run(lambda s: s.reverse(self.project_id, entry_id, key, effective_at=AT))
        assert reversal.reverses_entry_id == entry_id
        self.model = after
        self.reversible.remove(stored)

    @precondition(lambda self: self.history)
    @rule(pick=st.integers(min_value=0))
    def replay(self, pick: int) -> None:
        stored = self.history[pick % len(self.history)]
        (entry,) = self.run(lambda s: s.post_entries([stored.draft]))
        assert entry.id == stored.entry.id  # replay returns the stored entry, model unchanged

    # --- invariants ---------------------------------------------------------------------------

    @invariant()
    def ledger_is_consistent(self) -> None:
        if not self.model:
            return

        async def check(service: LedgerService) -> None:
            for currency in CURRENCIES:
                trial = await service.trial_balance(self.project_id, currency)
                assert trial.is_balanced, trial
            for account_id, expected in self.model.items():
                cached = await service.get_balance(self.project_id, account_id)
                assert cached == expected, (account_id, cached, expected)
                recomputed = await service.recompute_balance_from_postings(
                    self.project_id, account_id
                )
                assert recomputed == cached
                if account_id in self.no_overdraft:
                    assert not cached.available.is_negative()

        self.run(check)


def test_ledger_state_machine() -> None:
    with asyncio.Runner() as runner:
        engine = make_app_engine()
        LedgerMachine.runner = runner
        LedgerMachine.ledger = Ledger(create_session_factory(engine))
        try:
            run_state_machine_as_test(
                LedgerMachine,
                settings=settings(
                    max_examples=25,
                    stateful_step_count=25,
                    deadline=None,
                    suppress_health_check=[HealthCheck.too_slow],
                ),
            )
        finally:
            runner.run(engine.dispose())
