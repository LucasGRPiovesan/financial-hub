"""Public API of metering for other modules (ARCHITECTURE s3): import only from here.

Billing reads what a customer consumed through :func:`aggregate_usage`: one SQL aggregation over
the append-only usage events, exact (``numeric``), over the half-open range ``[start, end)``.

Integrations (manifests, ADR-0008) read and create metric definitions through
:func:`metric_definition` / :func:`define_metric` and record usage through :func:`record_usage`,
which is :meth:`MeteringService.ingest` (same idempotency and validation, same ``usage.recorded``).
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from financial_hub.modules.audit.application import AuditContext
from financial_hub.modules.events.application import EventPublisher
from financial_hub.modules.metering.application.service import (
    Ingested,
    MeteringService,
    MetricCodeTaken,
    UsageItem,
    UsageTransactionConflict,
    UsageView,
)
from financial_hub.modules.metering.domain import Aggregation, BillableMetric
from financial_hub.modules.metering.infrastructure import repository
from financial_hub.platform.db import UnitOfWork
from financial_hub.platform.http.auth import ProjectScope
from financial_hub.shared.clock import Clock
from financial_hub.shared.errors import NotFound

__all__ = [
    "Aggregation",
    "Ingested",
    "MetricCodeTaken",
    "MetricDefinition",
    "UsageAggregate",
    "UsageItem",
    "UsageTransactionConflict",
    "UsageView",
    "aggregate_usage",
    "define_metric",
    "metric_definition",
    "record_usage",
]


@dataclass(frozen=True, slots=True)
class UsageAggregate:
    """``value`` is ``None`` only for ``max`` over no events; ``events`` counts the range."""

    metric: str
    aggregation: Aggregation
    value: Decimal | None
    events: int


async def aggregate_usage(
    session: AsyncSession,
    *,
    project_id: UUID,
    customer_id: UUID,
    metric_code: str,
    start: datetime,
    end: datetime,
) -> UsageAggregate:
    """The metric's value for the customer identity over ``[start, end)`` (both timezone-aware).

    ``NotFound`` when the project has no such metric (archived metrics are still aggregated).
    """
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("start and end must be timezone-aware")
    metric = await repository.get_metric(session, project_id, metric_code)
    if metric is None:
        raise NotFound("Metric not found.")
    value, events = await repository.aggregate(
        session,
        project_id=project_id,
        customer_id=customer_id,
        metric=metric,
        start=start,
        end=end,
    )
    return UsageAggregate(
        metric=metric.code, aggregation=metric.aggregation, value=value, events=events
    )


@dataclass(frozen=True, slots=True)
class MetricDefinition:
    """How a metric aggregates: ``field`` is the property it reads (``None`` for ``count``)."""

    code: str
    name: str
    description: str | None
    aggregation: Aggregation
    field: str | None
    active: bool


def _definition(metric: BillableMetric) -> MetricDefinition:
    return MetricDefinition(
        code=metric.code,
        name=metric.name,
        description=metric.description,
        aggregation=metric.aggregation,
        field=metric.field,
        active=metric.is_active,
    )


async def metric_definition(
    session: AsyncSession, project_id: UUID, code: str
) -> MetricDefinition | None:
    """The project's metric ``code`` (archived ones included), or ``None``."""
    metric = await repository.get_metric(session, project_id, code)
    return None if metric is None else _definition(metric)


async def define_metric(
    uow: UnitOfWork,
    scope: ProjectScope,
    *,
    clock: Clock,
    audit: AuditContext,
    code: str,
    name: str,
    description: str | None,
    aggregation: str,
    field: str | None,
) -> MetricDefinition:
    """Create a metric (audited ``metric.created``); ``MetricCodeTaken`` when the code exists."""
    await MeteringService(uow, clock=clock, audit=audit).create_metric(
        scope,
        code=code,
        name=name,
        description=description,
        aggregation=aggregation,
        field=field,
    )
    created = await metric_definition(uow.session, scope.project_id, code)
    if created is None:  # pragma: no cover - inserted in this transaction
        raise NotFound("Metric not found.")
    return created


async def record_usage(
    uow: UnitOfWork,
    project_id: UUID,
    item: UsageItem,
    *,
    clock: Clock,
    publisher: EventPublisher | None = None,
) -> Ingested:
    """Idempotent ingestion of one usage event: :meth:`MeteringService.ingest` (``ValidationFailed``
    or ``UsageTransactionConflict``)."""
    return await MeteringService(uow, clock=clock, publisher=publisher).ingest(project_id, item)
