"""Read-only ledger queries reconciliation needs (ADR-0013): drift, trial balances, residues.

Each runs in the caller's transaction and never locks: a run reads one consistent snapshot.
"""

from collections.abc import Collection
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import RowMapping, bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession

_DRIFT = text(
    """
    WITH sums AS (
        SELECT p.account_id,
            coalesce(sum(p.amount) FILTER (WHERE e.status = 'posted' AND p.direction = 'debit'),
                     0)::bigint AS posted_dr,
            coalesce(sum(p.amount) FILTER (WHERE e.status = 'posted' AND p.direction = 'credit'),
                     0)::bigint AS posted_cr,
            coalesce(sum(p.amount) FILTER (WHERE e.status = 'pending' AND p.direction = 'debit'),
                     0)::bigint AS pending_dr,
            coalesce(sum(p.amount) FILTER (WHERE e.status = 'pending' AND p.direction = 'credit'),
                     0)::bigint AS pending_cr
        FROM public.ledger_postings p
        JOIN public.journal_entries e ON e.id = p.entry_id
        WHERE p.project_id = :project_id
        GROUP BY p.account_id
    )
    SELECT a.id, a.code, a.currency,
           a.posted_dr, a.posted_cr, a.pending_dr, a.pending_cr,
           coalesce(s.posted_dr, 0)::bigint AS re_posted_dr,
           coalesce(s.posted_cr, 0)::bigint AS re_posted_cr,
           coalesce(s.pending_dr, 0)::bigint AS re_pending_dr,
           coalesce(s.pending_cr, 0)::bigint AS re_pending_cr
    FROM public.ledger_accounts a
    LEFT JOIN sums s ON s.account_id = a.id
    WHERE a.project_id = :project_id
      AND (a.posted_dr <> coalesce(s.posted_dr, 0) OR a.posted_cr <> coalesce(s.posted_cr, 0)
           OR a.pending_dr <> coalesce(s.pending_dr, 0)
           OR a.pending_cr <> coalesce(s.pending_cr, 0))
    ORDER BY a.code, a.currency
    """
)

_TRIAL_BALANCES = text(
    """
    SELECT currency,
           coalesce(sum(posted_dr), 0)::bigint AS posted_dr,
           coalesce(sum(posted_cr), 0)::bigint AS posted_cr,
           coalesce(sum(pending_dr), 0)::bigint AS pending_dr,
           coalesce(sum(pending_cr), 0)::bigint AS pending_cr
    FROM public.ledger_accounts
    WHERE project_id = :project_id
    GROUP BY currency
    ORDER BY currency
    """
)

_RESIDUES = text(
    """
    SELECT e.correlation_id, a.code, a.currency,
           (coalesce(sum(p.amount) FILTER (WHERE p.direction = 'debit'), 0)
            - coalesce(sum(p.amount) FILTER (WHERE p.direction = 'credit'), 0))::bigint AS net_dr
    FROM public.journal_entries e
    JOIN public.ledger_postings p ON p.entry_id = e.id
    JOIN public.ledger_accounts a ON a.id = p.account_id
    WHERE e.project_id = :project_id
      AND e.status = 'posted'
      AND e.correlation_id = ANY(:correlation_ids)
      AND (a.code = :exact_code OR a.code LIKE :code_prefix)
    GROUP BY e.correlation_id, a.code, a.currency
    """
).bindparams(bindparam("correlation_ids", type_=ARRAY(PG_UUID(as_uuid=True))))


@dataclass(frozen=True, slots=True)
class Columns:
    """The four balance columns of one account (integer minor units)."""

    posted_dr: int
    posted_cr: int
    pending_dr: int
    pending_cr: int

    def as_dict(self) -> dict[str, int]:
        return {
            "posted_dr": self.posted_dr,
            "posted_cr": self.posted_cr,
            "pending_dr": self.pending_dr,
            "pending_cr": self.pending_cr,
        }


@dataclass(frozen=True, slots=True)
class AccountDrift:
    """An account whose materialized columns differ from the sums of its postings."""

    account_id: UUID
    code: str
    currency: str
    materialized: Columns
    recomputed: Columns


@dataclass(frozen=True, slots=True)
class CurrencyTrialBalance:
    currency: str
    totals: Columns

    @property
    def posted_net(self) -> int:
        return self.totals.posted_dr - self.totals.posted_cr

    @property
    def pending_net(self) -> int:
        return self.totals.pending_dr - self.totals.pending_cr


@dataclass(frozen=True, slots=True)
class Residue:
    """Net debit of posted entries correlated to one aggregate on one account."""

    correlation_id: UUID
    code: str
    currency: str
    net_debit: int


def _columns(row: RowMapping, prefix: str = "") -> Columns:
    return Columns(
        int(row[f"{prefix}posted_dr"]),
        int(row[f"{prefix}posted_cr"]),
        int(row[f"{prefix}pending_dr"]),
        int(row[f"{prefix}pending_cr"]),
    )


async def drifted_accounts(session: AsyncSession, project_id: UUID) -> list[AccountDrift]:
    rows = (await session.execute(_DRIFT, {"project_id": project_id})).mappings().all()
    return [
        AccountDrift(
            account_id=row["id"],
            code=row["code"],
            currency=row["currency"],
            materialized=_columns(row),
            recomputed=_columns(row, "re_"),
        )
        for row in rows
    ]


async def trial_balances(session: AsyncSession, project_id: UUID) -> list[CurrencyTrialBalance]:
    rows = (await session.execute(_TRIAL_BALANCES, {"project_id": project_id})).mappings().all()
    return [CurrencyTrialBalance(row["currency"], _columns(row)) for row in rows]


async def residues(
    session: AsyncSession,
    project_id: UUID,
    correlation_ids: Collection[UUID],
    *,
    exact_code: str,
    code_prefix: str,
) -> list[Residue]:
    """Per (correlation, account) net debit on ``exact_code`` and codes starting ``code_prefix``."""
    if not correlation_ids:
        return []
    params = {
        "project_id": project_id,
        "correlation_ids": list(correlation_ids),
        "exact_code": exact_code,
        "code_prefix": code_prefix.replace("%", r"\%").replace("_", r"\_") + "%",
    }
    rows = (await session.execute(_RESIDUES, params)).mappings().all()
    return [
        Residue(row["correlation_id"], row["code"], row["currency"], int(row["net_dr"]))
        for row in rows
    ]
