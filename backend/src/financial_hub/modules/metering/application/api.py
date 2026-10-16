"""Public API of metering for other modules (ARCHITECTURE s3): import only from here.

Billing reads what a customer consumed through :func:`aggregate_usage`: one SQL aggregation over
the append-only usage events, exact (``numeric``), over the half-open range ``[start, end)``.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from financial_hub.modules.metering.domain import Aggregation
from financial_hub.modules.metering.infrastructure import repository
from financial_hub.shared.errors import NotFound

__all__ = ["Aggregation", "UsageAggregate", "aggregate_usage"]


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
