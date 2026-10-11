"""Ledger persistence: plain statements on the caller's session (the Unit of Work owns the
transaction). Runs as ``fh_app``: inserts and reads only; balances move inside ``ledger_apply``.

Aggregates are cast to ``bigint`` in SQL so exact integers (never ``Decimal``/``float``) come back.
"""

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import RowMapping, select, text, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from financial_hub.modules.ledger.domain import (
    Account,
    AccountSpec,
    AccountType,
    Balance,
    Direction,
    EntryStatus,
    JournalEntry,
    JsonValue,
    Posting,
    TrialBalance,
)
from financial_hub.modules.ledger.infrastructure.tables import (
    journal_entries,
    ledger_accounts,
    ledger_postings,
)
from financial_hub.shared.ids import new_uuid
from financial_hub.shared.money import Currency, Money

type AccountKey = tuple[str, str]
"""``(code, currency)``: account identity inside a project."""

_LOCK_ACCOUNTS = text("SELECT * FROM public.ledger_lock_accounts(CAST(:ids AS uuid[]))")
_APPLY = text("SELECT public.ledger_apply(:entry_id)")
_RECOMPUTE = text(
    """
    SELECT
        coalesce(sum(p.amount) FILTER (WHERE e.status = 'posted' AND p.direction = 'debit'), 0)
            ::bigint AS posted_dr,
        coalesce(sum(p.amount) FILTER (WHERE e.status = 'posted' AND p.direction = 'credit'), 0)
            ::bigint AS posted_cr,
        coalesce(sum(p.amount) FILTER (WHERE e.status = 'pending' AND p.direction = 'debit'), 0)
            ::bigint AS pending_dr,
        coalesce(sum(p.amount) FILTER (WHERE e.status = 'pending' AND p.direction = 'credit'), 0)
            ::bigint AS pending_cr
    FROM public.ledger_postings p
    JOIN public.journal_entries e ON e.id = p.entry_id
    WHERE p.account_id = :account_id
    """
)
_TRIAL_BALANCE = text(
    """
    SELECT coalesce(sum(posted_dr), 0)::bigint AS posted_dr,
           coalesce(sum(posted_cr), 0)::bigint AS posted_cr,
           coalesce(sum(pending_dr), 0)::bigint AS pending_dr,
           coalesce(sum(pending_cr), 0)::bigint AS pending_cr
    FROM public.ledger_accounts
    WHERE project_id = :project_id AND currency = :currency
    """
)


def _account(row: RowMapping) -> Account:
    currency = Currency.of(row["currency"])
    spec = AccountSpec(
        code=row["code"],
        type=AccountType(row["type"]),
        no_overdraft=row["no_overdraft"],
        customer_id=row["customer_id"],
        payee_id=row["payee_id"],
        provider_account_id=row["provider_account_id"],
    )
    balance = Balance(
        currency,
        spec.normal_balance,
        row["posted_dr"],
        row["posted_cr"],
        row["pending_dr"],
        row["pending_cr"],
    )
    return Account(row["id"], row["project_id"], spec, currency, balance, row["version"])


async def insert_missing_accounts(
    session: AsyncSession, project_id: UUID, specs: Mapping[AccountKey, AccountSpec]
) -> None:
    """Provision accounts (``INSERT ... ON CONFLICT DO NOTHING``) in a deterministic order."""
    if not specs:
        return
    rows = [
        {
            "id": account_id,
            "project_id": project_id,
            "code": code,
            "currency": currency,
            "type": spec.type.value,
            "no_overdraft": spec.no_overdraft,
            "customer_id": spec.customer_id,
            "payee_id": spec.payee_id,
            "provider_account_id": spec.provider_account_id,
        }
        for account_id, ((code, currency), spec) in zip(
            (new_uuid() for _ in specs), sorted(specs.items()), strict=True
        )
    ]
    statement = (
        insert(ledger_accounts)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["project_id", "code", "currency"])
    )
    await session.execute(statement)


async def account_ids(
    session: AsyncSession, project_id: UUID, keys: Iterable[AccountKey]
) -> dict[AccountKey, UUID]:
    wanted = sorted(set(keys))
    if not wanted:
        return {}
    statement = select(
        ledger_accounts.c.id, ledger_accounts.c.code, ledger_accounts.c.currency
    ).where(
        ledger_accounts.c.project_id == project_id,
        tuple_(ledger_accounts.c.code, ledger_accounts.c.currency).in_(wanted),
    )
    result = await session.execute(statement)
    return {(row.code, row.currency): row.id for row in result}


async def lock_accounts(session: AsyncSession, ids: Iterable[UUID]) -> dict[UUID, Account]:
    """``SELECT ... FOR UPDATE`` in id order through ``ledger_lock_accounts`` (ADR-0003 s5)."""
    wanted = sorted(set(ids))
    if not wanted:
        return {}
    result = await session.execute(_LOCK_ACCOUNTS, {"ids": wanted})
    return {account.id: account for account in map(_account, result.mappings())}


async def get_account(session: AsyncSession, project_id: UUID, account_id: UUID) -> Account | None:
    statement = select(ledger_accounts).where(
        ledger_accounts.c.id == account_id, ledger_accounts.c.project_id == project_id
    )
    row = (await session.execute(statement)).mappings().one_or_none()
    return None if row is None else _account(row)


async def get_accounts(
    session: AsyncSession, project_id: UUID, ids: Iterable[UUID]
) -> dict[UUID, Account]:
    wanted = sorted(set(ids))
    if not wanted:
        return {}
    statement = select(ledger_accounts).where(
        ledger_accounts.c.project_id == project_id, ledger_accounts.c.id.in_(wanted)
    )
    result = await session.execute(statement)
    return {account.id: account for account in map(_account, result.mappings())}


async def find_account(
    session: AsyncSession, project_id: UUID, code: str, currency: Currency
) -> Account | None:
    statement = select(ledger_accounts).where(
        ledger_accounts.c.project_id == project_id,
        ledger_accounts.c.code == code,
        ledger_accounts.c.currency == currency.code,
    )
    row = (await session.execute(statement)).mappings().one_or_none()
    return None if row is None else _account(row)


async def entry_ids_by_key(
    session: AsyncSession, project_id: UUID, keys: Iterable[str]
) -> dict[str, tuple[UUID, str]]:
    """``idempotency_key -> (entry id, payload_hash)`` of the existing entries."""
    wanted = sorted(set(keys))
    if not wanted:
        return {}
    statement = select(
        journal_entries.c.idempotency_key, journal_entries.c.id, journal_entries.c.payload_hash
    ).where(
        journal_entries.c.project_id == project_id,
        journal_entries.c.idempotency_key.in_(wanted),
    )
    result = await session.execute(statement)
    return {row.idempotency_key: (row.id, row.payload_hash) for row in result}


async def reversal_of(session: AsyncSession, entry_id: UUID) -> UUID | None:
    statement = select(journal_entries.c.id).where(journal_entries.c.reverses_entry_id == entry_id)
    return (await session.execute(statement)).scalar_one_or_none()


async def insert_entry(
    session: AsyncSession,
    *,
    entry_id: UUID,
    project_id: UUID,
    status: EntryStatus,
    idempotency_key: str,
    payload_hash: str,
    recipe: str,
    effective_at: datetime,
    reverses_entry_id: UUID | None,
    correlation_id: UUID | None,
    causation_id: UUID | None,
    metadata: Mapping[str, object],
) -> bool:
    """``False`` when the idempotency key already exists (a concurrent writer won)."""
    statement = (
        insert(journal_entries)
        .values(
            id=entry_id,
            project_id=project_id,
            status=status.value,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            recipe=recipe,
            effective_at=effective_at,
            reverses_entry_id=reverses_entry_id,
            correlation_id=correlation_id,
            causation_id=causation_id,
            metadata=dict(metadata),
        )
        .on_conflict_do_nothing(index_elements=["project_id", "idempotency_key"])
        .returning(journal_entries.c.id)
    )
    return (await session.execute(statement)).scalar_one_or_none() is not None


async def insert_postings(
    session: AsyncSession,
    *,
    entry_id: UUID,
    project_id: UUID,
    postings: Sequence[Posting],
) -> None:
    rows = [
        {
            "id": posting.id,
            "entry_id": entry_id,
            "project_id": project_id,
            "account_id": posting.account_id,
            "currency": posting.amount.currency.code,
            "direction": posting.direction.value,
            "amount": posting.amount.minor_units,
        }
        for posting in postings
    ]
    await session.execute(insert(ledger_postings).values(rows))


async def apply(session: AsyncSession, entry_id: UUID) -> int:
    """Run ``ledger_apply``: moves balances to the entry's status. Returns accounts touched."""
    return (await session.execute(_APPLY, {"entry_id": entry_id})).scalar_one()


async def set_status(session: AsyncSession, entry_id: UUID, target: EntryStatus) -> bool:
    """``pending -> target``; ``False`` if the entry is no longer pending."""
    statement = (
        journal_entries.update()
        .where(journal_entries.c.id == entry_id, journal_entries.c.status == "pending")
        .values(status=target.value)
        .returning(journal_entries.c.id)
    )
    return (await session.execute(statement)).scalar_one_or_none() is not None


async def entry_status(session: AsyncSession, entry_id: UUID) -> EntryStatus | None:
    statement = select(journal_entries.c.status).where(journal_entries.c.id == entry_id)
    value = (await session.execute(statement)).scalar_one_or_none()
    return None if value is None else EntryStatus(value)


async def load_entries(
    session: AsyncSession, project_id: UUID, ids: Iterable[UUID]
) -> dict[UUID, JournalEntry]:
    wanted = sorted(set(ids))
    if not wanted:
        return {}
    entry_rows = (
        await session.execute(
            select(journal_entries).where(
                journal_entries.c.project_id == project_id, journal_entries.c.id.in_(wanted)
            )
        )
    ).mappings()
    posting_rows = (
        await session.execute(
            select(ledger_postings)
            .where(
                ledger_postings.c.project_id == project_id,
                ledger_postings.c.entry_id.in_(wanted),
            )
            .order_by(ledger_postings.c.id)
        )
    ).mappings()
    postings: dict[UUID, list[Posting]] = {}
    for row in posting_rows:
        postings.setdefault(row["entry_id"], []).append(
            Posting(
                row["id"],
                row["account_id"],
                Direction(row["direction"]),
                Money(row["amount"], Currency.of(row["currency"])),
            )
        )
    entries: dict[UUID, JournalEntry] = {}
    for row in entry_rows:
        metadata: dict[str, JsonValue] = row["metadata"]
        entries[row["id"]] = JournalEntry(
            id=row["id"],
            project_id=row["project_id"],
            status=EntryStatus(row["status"]),
            idempotency_key=row["idempotency_key"],
            payload_hash=row["payload_hash"],
            recipe=row["recipe"],
            effective_at=row["effective_at"],
            postings=tuple(postings.get(row["id"], ())),
            reverses_entry_id=row["reverses_entry_id"],
            correlation_id=row["correlation_id"],
            causation_id=row["causation_id"],
            metadata=metadata,
        )
    return entries


def _sums(row: RowMapping) -> dict[str, Any]:
    return {
        "posted_dr": int(row["posted_dr"]),
        "posted_cr": int(row["posted_cr"]),
        "pending_dr": int(row["pending_dr"]),
        "pending_cr": int(row["pending_cr"]),
    }


async def recompute_balance(session: AsyncSession, account: Account) -> Balance:
    """Balance of ``account`` rebuilt from its postings and their entries' status."""
    row = (await session.execute(_RECOMPUTE, {"account_id": account.id})).mappings().one()
    return Balance(account.currency, account.normal_balance, **_sums(row))


async def trial_balance(
    session: AsyncSession, project_id: UUID, currency: Currency
) -> TrialBalance:
    params = {"project_id": project_id, "currency": currency.code}
    row = (await session.execute(_TRIAL_BALANCE, params)).mappings().one()
    return TrialBalance(currency, **_sums(row))
