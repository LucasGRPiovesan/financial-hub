"""Audit persistence on the caller's session (runs as ``fh_app``: INSERT and SELECT only)."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from financial_hub.modules.audit.infrastructure.tables import audit_records
from financial_hub.shared.pagination import Cursor, keyset_predicate


@dataclass(frozen=True, slots=True)
class AuditRow:
    id: UUID
    project_id: UUID | None
    actor_type: str
    actor_id: str | None
    action: str
    target_type: str
    target_id: str | None
    diff: dict[str, Any] | None
    correlation_id: str | None
    request_id: str | None
    ip: str | None
    user_agent: str | None
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class AuditFilters:
    action: str | None = None
    actor_id: str | None = None
    target_type: str | None = None
    target_id: str | None = None
    occurred_from: datetime | None = None
    occurred_to: datetime | None = None


async def insert_record(session: AsyncSession, values: dict[str, Any]) -> None:
    await session.execute(insert(audit_records).values(**values))


def _row(row: Any) -> AuditRow:
    return AuditRow(
        id=row.id,
        project_id=row.project_id,
        actor_type=row.actor_type,
        actor_id=row.actor_id,
        action=row.action,
        target_type=row.target_type,
        target_id=row.target_id,
        diff=row.diff,
        correlation_id=row.correlation_id,
        request_id=row.request_id,
        ip=None if row.ip is None else str(row.ip),
        user_agent=row.user_agent,
        occurred_at=row.occurred_at,
    )


async def list_records(
    session: AsyncSession,
    project_id: UUID,
    filters: AuditFilters,
    *,
    cursor: Cursor | None,
    limit: int,
) -> Sequence[AuditRow]:
    """Newest first (``occurred_at``, ``id``); callers pass ``limit + 1`` to detect more."""
    table = audit_records
    statement = select(table).where(table.c.project_id == project_id)
    for column, value in (
        (table.c.action, filters.action),
        (table.c.actor_id, filters.actor_id),
        (table.c.target_type, filters.target_type),
        (table.c.target_id, filters.target_id),
    ):
        if value is not None:
            statement = statement.where(column == value)
    if filters.occurred_from is not None:
        statement = statement.where(table.c.occurred_at >= filters.occurred_from)
    if filters.occurred_to is not None:
        statement = statement.where(table.c.occurred_at < filters.occurred_to)
    if cursor is not None:
        statement = statement.where(keyset_predicate(table.c.occurred_at, table.c.id, cursor))
    statement = statement.order_by(table.c.occurred_at.desc(), table.c.id.desc()).limit(limit)
    return [_row(row) for row in (await session.execute(statement)).all()]
