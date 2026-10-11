"""Shared helpers for ledger tests against real Postgres.

Ledger tests connect as ``fh_app`` (``FH_APP_TEST_DATABASE_URL``, set by the compose ``test``
service) so they prove the runtime privileges, not the owner's. Rows cannot be deleted (append-
only), so every test isolates itself with a fresh ``project_id`` instead of cleaning up.
"""

import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from financial_hub.modules.ledger.application import (
    AccountSpec,
    AccountType,
    Balance,
    Direction,
    EntryDraft,
    JournalEntry,
    LedgerService,
    PostingDraft,
)
from financial_hub.platform.db import UnitOfWork
from financial_hub.shared.money import BRL, Currency, Money

APP_URL_ENV = "FH_APP_TEST_DATABASE_URL"
AT = datetime(2026, 10, 10, 12, tzinfo=UTC)

CASH = AccountSpec("platform:cash", AccountType.ASSET)
REVENUE = AccountSpec("platform:revenue", AccountType.INCOME)
FEES = AccountSpec("platform:fees_expense", AccountType.EXPENSE)
PAYABLE = AccountSpec("payee:payable", AccountType.LIABILITY)
WALLET = AccountSpec("customer:wallet", AccountType.LIABILITY, no_overdraft=True)


def app_database_url() -> str:
    url = os.environ.get(APP_URL_ENV)
    if not url:
        pytest.fail(f"{APP_URL_ENV} is not set: run the suite with scripts/test.sh")
    return url


def make_app_engine(pool_size: int = 5) -> AsyncEngine:
    return create_async_engine(app_database_url(), pool_size=pool_size, max_overflow=5)


def leg(
    spec: AccountSpec, direction: Direction, units: int, currency: Currency = BRL
) -> PostingDraft:
    return PostingDraft(spec, direction, Money(units, currency))


def transfer(
    project_id: UUID,
    key: str,
    debit: AccountSpec,
    credit: AccountSpec,
    units: int,
    *,
    currency: Currency = BRL,
    pending: bool = False,
    recipe: str = "test",
) -> EntryDraft:
    draft = EntryDraft.create(
        project_id=project_id,
        idempotency_key=key,
        recipe=recipe,
        postings=[
            leg(debit, Direction.DEBIT, units, currency),
            leg(credit, Direction.CREDIT, units, currency),
        ],
        effective_at=AT,
        pending=pending,
    )
    assert draft is not None
    return draft


class Ledger:
    """Runs one service call per unit of work (one transaction), like a command handler."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory

    def uow(self) -> UnitOfWork:
        return UnitOfWork(self.factory)

    async def run[T](self, action: Callable[[LedgerService], Awaitable[T]]) -> T:
        async with self.uow() as uow:
            return await action(LedgerService(uow))

    async def post(self, *drafts: EntryDraft) -> list[JournalEntry]:
        return await self.run(lambda ledger: ledger.post_entries(drafts))

    async def balance(
        self, project_id: UUID, spec: AccountSpec, currency: Currency = BRL
    ) -> Balance:
        async def read(ledger: LedgerService) -> Balance:
            account = await ledger.find_account(project_id, spec.code, currency)
            assert account is not None, spec
            return account.balance

        return await self.run(read)

    async def account_ids(self, project_id: UUID) -> list[UUID]:
        async with self.factory() as session:
            rows = await session.execute(
                text("SELECT id FROM ledger_accounts WHERE project_id = :p ORDER BY id"),
                {"p": project_id},
            )
            return list(rows.scalars())

    async def assert_consistent(self, project_id: UUID, *currencies: Currency) -> None:
        """Trial balance is zero per currency; every cached balance equals its recomputation."""
        ids = await self.account_ids(project_id)

        async def check(ledger: LedgerService) -> None:
            for account_id in ids:
                cached = await ledger.get_balance(project_id, account_id)
                recomputed = await ledger.recompute_balance_from_postings(project_id, account_id)
                assert cached == recomputed, account_id
                account = await ledger.get_account(project_id, account_id)
                if account.no_overdraft:
                    assert not cached.available.is_negative(), account
            for currency in currencies or (BRL,):
                trial = await ledger.trial_balance(project_id, currency)
                assert trial.is_balanced, trial

        await self.run(check)
