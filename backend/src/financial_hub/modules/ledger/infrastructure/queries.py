"""Read-only SQL of the ledger query API (PRO-36): plain selects on the caller's session.

Every statement filters by ``project_id`` (a row of another project is simply not found) and only
reads: no locks, no writes. Aggregates are cast to ``bigint`` so exact integers come back.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import ColumnElement, RowMapping, Select, and_, exists, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from financial_hub.modules.ledger.infrastructure.tables import (
    journal_entries,
    ledger_accounts,
    ledger_postings,
)
from financial_hub.shared.pagination import Cursor, keyset_predicate

type OwnerColumn = Literal["customer_id", "payee_id", "provider_account_id"]

_AS_OF_SUMS = text(
    """
    SELECT coalesce(sum(p.amount) FILTER (WHERE p.direction = 'debit'), 0)::bigint AS debits,
           coalesce(sum(p.amount) FILTER (WHERE p.direction = 'credit'), 0)::bigint AS credits
    FROM public.ledger_postings p
    JOIN public.journal_entries e ON e.id = p.entry_id AND e.project_id = p.project_id
    WHERE p.project_id = :project_id AND p.account_id = :account_id
      AND e.status = 'posted' AND e.effective_at <= :as_of
    """
)
_TRIAL_CURRENT = text(
    """
    SELECT currency, type, split_part(code, ':', 1) AS code,
           coalesce(sum(posted_dr), 0)::bigint AS debits,
           coalesce(sum(posted_cr), 0)::bigint AS credits
    FROM public.ledger_accounts
    WHERE project_id = :project_id AND (CAST(:currency AS text) IS NULL OR currency = :currency)
    GROUP BY currency, type, split_part(code, ':', 1)
    HAVING coalesce(sum(posted_dr), 0) <> 0 OR coalesce(sum(posted_cr), 0) <> 0
    ORDER BY currency, type, split_part(code, ':', 1)
    """
)
_TRIAL_AS_OF = text(
    """
    SELECT a.currency, a.type, split_part(a.code, ':', 1) AS code,
           coalesce(sum(p.amount) FILTER (WHERE p.direction = 'debit'), 0)::bigint AS debits,
           coalesce(sum(p.amount) FILTER (WHERE p.direction = 'credit'), 0)::bigint AS credits
    FROM public.ledger_postings p
    JOIN public.journal_entries e ON e.id = p.entry_id AND e.project_id = p.project_id
    JOIN public.ledger_accounts a ON a.id = p.account_id AND a.project_id = p.project_id
    WHERE p.project_id = :project_id AND (CAST(:currency AS text) IS NULL OR a.currency = :currency)
      AND e.status = 'posted' AND e.effective_at <= :as_of
    GROUP BY a.currency, a.type, split_part(a.code, ':', 1)
    ORDER BY a.currency, a.type, split_part(a.code, ':', 1)
    """
)


async def list_accounts(
    session: AsyncSession,
    project_id: UUID,
    *,
    code: str | None,
    account_type: str | None,
    owner: tuple[OwnerColumn, UUID] | None,
    currency: str | None,
    cursor: Cursor | None,
    limit: int,
) -> Sequence[RowMapping]:
    """Accounts oldest first (``created_at, id``); ``limit + 1`` rows to detect another page."""
    accounts = ledger_accounts
    conditions: list[ColumnElement[bool]] = [accounts.c.project_id == project_id]
    if code is not None:
        conditions.append(accounts.c.code == code)
    if account_type is not None:
        conditions.append(accounts.c.type == account_type)
    if owner is not None:
        conditions.append(accounts.c[owner[0]] == owner[1])
    if currency is not None:
        conditions.append(accounts.c.currency == currency)
    if cursor is not None:
        conditions.append(keyset_predicate(accounts.c.created_at, accounts.c.id, cursor, "asc"))
    statement = (
        select(accounts)
        .where(and_(*conditions))
        .order_by(accounts.c.created_at.asc(), accounts.c.id.asc())
        .limit(limit + 1)
    )
    return (await session.execute(statement)).mappings().all()


async def get_account(
    session: AsyncSession, project_id: UUID, account_id: UUID
) -> RowMapping | None:
    statement = select(ledger_accounts).where(
        ledger_accounts.c.project_id == project_id, ledger_accounts.c.id == account_id
    )
    return (await session.execute(statement)).mappings().first()


async def posted_sums_as_of(
    session: AsyncSession, project_id: UUID, account_id: UUID, as_of: datetime
) -> tuple[int, int]:
    """``(debits, credits)`` of the account's postings in posted entries effective <= ``as_of``."""
    result = await session.execute(
        _AS_OF_SUMS, {"project_id": project_id, "account_id": account_id, "as_of": as_of}
    )
    row = result.mappings().one()
    return row["debits"], row["credits"]


def _entries_with_reversal(project_id: UUID) -> Select[tuple[object, ...]]:
    """Entries joined with the id of the entry that reverses them (``reversed_by_entry_id``)."""
    reversal = aliased(journal_entries)
    return select(journal_entries, reversal.c.id.label("reversed_by_entry_id")).outerjoin(
        reversal,
        and_(
            reversal.c.reverses_entry_id == journal_entries.c.id,
            reversal.c.project_id == project_id,
        ),
    )


async def list_entries(
    session: AsyncSession,
    project_id: UUID,
    *,
    account_id: UUID | None,
    recipe: str | None,
    status: str | None,
    correlation_id: UUID | None,
    effective_from: datetime | None,
    effective_to: datetime | None,
    cursor: Cursor | None,
    limit: int,
) -> Sequence[RowMapping]:
    """Entries newest first (``created_at desc, id desc``); ``limit + 1`` rows."""
    entries = journal_entries
    conditions: list[ColumnElement[bool]] = [entries.c.project_id == project_id]
    if account_id is not None:
        conditions.append(
            exists().where(
                ledger_postings.c.entry_id == entries.c.id,
                ledger_postings.c.project_id == project_id,
                ledger_postings.c.account_id == account_id,
            )
        )
    if recipe is not None:
        conditions.append(entries.c.recipe == recipe)
    if status is not None:
        conditions.append(entries.c.status == status)
    if correlation_id is not None:
        conditions.append(entries.c.correlation_id == correlation_id)
    if effective_from is not None:
        conditions.append(entries.c.effective_at >= effective_from)
    if effective_to is not None:
        conditions.append(entries.c.effective_at <= effective_to)
    if cursor is not None:
        conditions.append(keyset_predicate(entries.c.created_at, entries.c.id, cursor, "desc"))
    statement = (
        _entries_with_reversal(project_id)
        .where(and_(*conditions))
        .order_by(entries.c.created_at.desc(), entries.c.id.desc())
        .limit(limit + 1)
    )
    return (await session.execute(statement)).mappings().all()


async def get_entry(session: AsyncSession, project_id: UUID, entry_id: UUID) -> RowMapping | None:
    statement = _entries_with_reversal(project_id).where(
        journal_entries.c.project_id == project_id, journal_entries.c.id == entry_id
    )
    return (await session.execute(statement)).mappings().first()


async def entry_postings(
    session: AsyncSession, project_id: UUID, entry_id: UUID
) -> Sequence[RowMapping]:
    """Postings of one entry with the code of their account, in a stable order."""
    postings = ledger_postings
    statement = (
        select(
            postings.c.id,
            postings.c.account_id,
            ledger_accounts.c.code.label("account_code"),
            postings.c.direction,
            postings.c.amount,
            postings.c.currency,
        )
        .join(ledger_accounts, ledger_accounts.c.id == postings.c.account_id)
        .where(postings.c.project_id == project_id, postings.c.entry_id == entry_id)
        .order_by(postings.c.direction.asc(), ledger_accounts.c.code.asc(), postings.c.id.asc())
    )
    return (await session.execute(statement)).mappings().all()


async def trial_balance_lines(
    session: AsyncSession, project_id: UUID, currency: str | None, as_of: datetime | None
) -> Sequence[RowMapping]:
    """Posted debits/credits per ``(currency, type, code family)``.

    Without ``as_of`` the materialized account columns are summed; with it the totals are
    recomputed from the postings of posted entries effective at or before ``as_of``.
    """
    params: dict[str, object] = {"project_id": project_id, "currency": currency}
    if as_of is None:
        return (await session.execute(_TRIAL_CURRENT, params)).mappings().all()
    return (await session.execute(_TRIAL_AS_OF, {**params, "as_of": as_of})).mappings().all()
