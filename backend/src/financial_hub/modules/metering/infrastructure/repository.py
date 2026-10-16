"""Metering persistence on the caller's session (runs as ``fh_app``).

``usage_events`` is append-only (INSERT and SELECT only) and every read is scoped by
``project_id``. Aggregations are computed by PostgreSQL over ``numeric`` values, never in Python
floats.
"""

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from financial_hub.modules.metering.domain import (
    Aggregation,
    BillableMetric,
    MetricStatus,
    UsageEvent,
)
from financial_hub.modules.metering.infrastructure.tables import billable_metrics, usage_events
from financial_hub.shared.pagination import Cursor, keyset_predicate

_metric = billable_metrics
_event = usage_events


def _to_metric(row: Any) -> BillableMetric:
    return BillableMetric(
        id=row.id,
        project_id=row.project_id,
        code=row.code,
        name=row.name,
        description=row.description,
        aggregation=Aggregation(row.aggregation),
        field=row.field,
        status=MetricStatus(row.status),
        created_at=row.created_at,
        updated_at=row.updated_at,
        archived_at=row.archived_at,
    )


def _to_event(row: Any) -> UsageEvent:
    return UsageEvent(
        id=row.id,
        project_id=row.project_id,
        customer_id=row.customer_id,
        metric_code=row.metric_code,
        transaction_id=row.transaction_id,
        occurred_at=row.occurred_at,
        properties=dict(row.properties),
        value=row.value,
        unique_value=row.unique_value,
        ingested_at=row.ingested_at,
    )


async def insert_metric(
    session: AsyncSession,
    *,
    metric_id: UUID,
    project_id: UUID,
    code: str,
    name: str,
    description: str | None,
    aggregation: Aggregation,
    field: str | None,
    now: datetime,
) -> bool:
    """``False`` when the project already has a metric with this code."""
    statement = (
        insert(_metric)
        .values(
            id=metric_id,
            project_id=project_id,
            code=code,
            name=name,
            description=description,
            aggregation=aggregation.value,
            field=field,
            status=MetricStatus.ACTIVE.value,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=[_metric.c.project_id, _metric.c.code])
        .returning(_metric.c.id)
    )
    return (await session.execute(statement)).first() is not None


async def get_metric(session: AsyncSession, project_id: UUID, code: str) -> BillableMetric | None:
    statement = select(_metric).where(_metric.c.project_id == project_id, _metric.c.code == code)
    row = (await session.execute(statement)).first()
    return None if row is None else _to_metric(row)


async def list_metrics(
    session: AsyncSession,
    project_id: UUID,
    *,
    status: MetricStatus | None,
    cursor: Cursor | None,
    limit: int,
) -> Sequence[BillableMetric]:
    """Newest first, ``limit`` rows at most (callers pass ``limit + 1`` to detect more)."""
    statement = select(_metric).where(_metric.c.project_id == project_id)
    if status is not None:
        statement = statement.where(_metric.c.status == status.value)
    if cursor is not None:
        statement = statement.where(keyset_predicate(_metric.c.created_at, _metric.c.id, cursor))
    statement = statement.order_by(_metric.c.created_at.desc(), _metric.c.id.desc()).limit(limit)
    return [_to_metric(row) for row in (await session.execute(statement)).all()]


async def archive_metric(
    session: AsyncSession, project_id: UUID, code: str, now: datetime
) -> BillableMetric | None:
    """Archive an active metric; ``None`` when it is not active (unknown or already archived)."""
    statement = (
        update(_metric)
        .where(
            _metric.c.project_id == project_id,
            _metric.c.code == code,
            _metric.c.status == MetricStatus.ACTIVE.value,
        )
        .values(status=MetricStatus.ARCHIVED.value, archived_at=now, updated_at=now)
        .returning(*_metric.c)
    )
    row = (await session.execute(statement)).first()
    return None if row is None else _to_metric(row)


async def get_event(
    session: AsyncSession, project_id: UUID, transaction_id: str
) -> UsageEvent | None:
    statement = select(_event).where(
        _event.c.project_id == project_id, _event.c.transaction_id == transaction_id
    )
    row = (await session.execute(statement)).first()
    return None if row is None else _to_event(row)


async def insert_event(session: AsyncSession, event: UsageEvent) -> bool:
    """``False`` when ``(project, transaction_id)`` already exists (the idempotency key)."""
    statement = (
        insert(_event)
        .values(
            id=event.id,
            project_id=event.project_id,
            customer_id=event.customer_id,
            metric_code=event.metric_code,
            transaction_id=event.transaction_id,
            occurred_at=event.occurred_at,
            properties=event.properties,
            value=event.value,
            unique_value=event.unique_value,
            ingested_at=event.ingested_at,
        )
        .on_conflict_do_nothing(index_elements=[_event.c.project_id, _event.c.transaction_id])
        .returning(_event.c.id)
    )
    return (await session.execute(statement)).first() is not None


async def aggregate(
    session: AsyncSession,
    *,
    project_id: UUID,
    customer_id: UUID,
    metric: BillableMetric,
    start: datetime,
    end: datetime,
) -> tuple[Decimal | None, int]:
    """``(value, events)`` of ``metric`` for the customer over ``[start, end)``.

    ``value`` is computed by SQL: ``SUM``/``MAX`` of the exact ``numeric`` column, ``COUNT(*)`` or
    ``COUNT(DISTINCT unique_value)``. ``SUM``, ``COUNT`` and ``COUNT DISTINCT`` of nothing are 0;
    ``MAX`` of nothing is ``None``.
    """
    match metric.aggregation:
        case Aggregation.SUM:
            expression = func.coalesce(func.sum(_event.c.value), Decimal(0))
        case Aggregation.MAX:
            expression = func.max(_event.c.value)
        case Aggregation.COUNT:
            expression = func.count()
        case Aggregation.UNIQUE_COUNT:
            expression = func.count(_event.c.unique_value.distinct())
    statement = select(expression.label("value"), func.count().label("events")).where(
        _event.c.project_id == project_id,
        _event.c.customer_id == customer_id,
        _event.c.metric_code == metric.code,
        _event.c.occurred_at >= start,
        _event.c.occurred_at < end,
    )
    row = (await session.execute(statement)).one()
    value = row.value
    return (None if value is None else Decimal(value)), int(row.events)
