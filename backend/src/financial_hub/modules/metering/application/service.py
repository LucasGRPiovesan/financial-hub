"""Metering use cases: billable metrics, idempotent usage ingestion, aggregation (docs/METERING.md).

Ingestion decides each event in this order, which is what makes retries safe:

1. the shape (transaction id, properties, timezone) and the customer;
2. an event with the same ``(project, transaction_id)`` already stored: the *same payload* is a
   duplicate (the original is returned, nothing is written), a *different payload* is a 409;
3. only for a new transaction: the metric (known and active), the measure its aggregation reads, the
   timestamp window; then the INSERT (``ON CONFLICT DO NOTHING``; a concurrent winner is re-read and
   compared as in step 2) and the ``usage.recorded`` event in the same transaction.

Looking at the stored event before the metric and the window means a legitimate retry still
succeeds after the metric was archived or the event aged out of the window.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from financial_hub.modules.audit.application import (
    Actor,
    AuditContext,
    AuditRecorder,
    AuditTarget,
)
from financial_hub.modules.customers.application.api import resolve_customer_identity
from financial_hub.modules.events.application import EventDraft, EventPublisher
from financial_hub.modules.metering.application import events as metering_events
from financial_hub.modules.metering.domain import (
    MAX_BATCH,
    BillableMetric,
    MetricStatus,
    UsageEvent,
    check_window,
    measure_of,
    plain_decimal,
    same_payload,
    validate_code,
    validate_definition,
    validate_description,
    validate_name,
    validate_properties,
    validate_transaction_id,
)
from financial_hub.modules.metering.infrastructure import repository
from financial_hub.platform.db import UnitOfWork
from financial_hub.platform.http.auth import ProjectScope
from financial_hub.shared.clock import Clock
from financial_hub.shared.errors import Conflict, FieldError, NotFound, ValidationFailed
from financial_hub.shared.ids import IdPrefix, format_id, new_uuid
from financial_hub.shared.pagination import Cursor, Page, PageParams, build_page

_EVENT_TYPES = metering_events  # importing the module registers the event types
BATCH_LIMIT: Final = MAX_BATCH  # events per batch request


@dataclass(frozen=True, slots=True)
class MetricView:
    code: str
    name: str
    description: str | None
    aggregation: str
    field: str | None
    status: str
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None


@dataclass(frozen=True, slots=True)
class UsageView:
    transaction_id: str
    customer_id: str
    metric: str
    timestamp: datetime
    properties: dict[str, Any]
    value: str | None
    ingested_at: datetime


@dataclass(frozen=True, slots=True)
class UsageItem:
    """One usage event as sent by the caller (not yet validated)."""

    transaction_id: str
    metric: str
    properties: dict[str, Any]
    timestamp: datetime | None = None
    customer_id: str | None = None
    customer_external_ref: str | None = None


@dataclass(frozen=True, slots=True)
class Ingested:
    usage: UsageView
    created: bool


@dataclass(frozen=True, slots=True)
class ItemResult:
    """Outcome of one event of a batch: ``ingested`` or the ``error`` that rejected it."""

    ingested: Ingested | None = None
    error: Conflict | ValidationFailed | None = None


@dataclass(frozen=True, slots=True)
class UsageSummary:
    customer_id: str
    metric: str
    aggregation: str
    start: datetime
    end: datetime
    value: str | None
    events: int


class MetricCodeTaken(Conflict):
    """The project already has a metric with this code (409)."""

    slug = "metric-code-taken"
    title = "Metric code already in use"


class UsageTransactionConflict(Conflict):
    """The transaction id was already recorded with a different payload (409)."""

    slug = "usage-transaction-conflict"
    title = "Transaction id reused with a different usage event"


def _invalid(pointer: str, detail: str, code: str = "invalid") -> ValidationFailed:
    return ValidationFailed(
        "The request contains invalid fields.",
        errors=[FieldError(pointer=pointer, detail=detail, code=code)],
    )


def _metric_view(metric: BillableMetric) -> MetricView:
    return MetricView(
        code=metric.code,
        name=metric.name,
        description=metric.description,
        aggregation=metric.aggregation.value,
        field=metric.field,
        status=metric.status.value,
        created_at=metric.created_at,
        updated_at=metric.updated_at,
        archived_at=metric.archived_at,
    )


def _usage_view(event: UsageEvent) -> UsageView:
    return UsageView(
        transaction_id=event.transaction_id,
        customer_id=format_id(IdPrefix.CUSTOMER, event.customer_id),
        metric=event.metric_code,
        timestamp=event.occurred_at,
        properties=event.properties,
        value=None if event.value is None else plain_decimal(event.value),
        ingested_at=event.ingested_at,
    )


class MeteringService:
    """Every method runs inside the caller's active :class:`UnitOfWork`."""

    def __init__(
        self,
        uow: UnitOfWork,
        *,
        clock: Clock,
        audit: AuditContext | None = None,
        publisher: EventPublisher | None = None,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._audit = audit or AuditContext.system()
        self._publisher = publisher or EventPublisher(clock=clock, notifier=None)

    # -- metrics ---------------------------------------------------------------------------

    async def _record(
        self, scope: ProjectScope, action: str, code: str, diff: dict[str, Any]
    ) -> None:
        await AuditRecorder.record(
            self._uow.session,
            actor=self._audit.acting(Actor.of_principal(scope.principal_kind, scope.principal_id)),
            action=action,
            target=AuditTarget("billable_metric", code),
            diff=diff,
            project_id=scope.project_id,
        )

    async def create_metric(
        self,
        scope: ProjectScope,
        *,
        code: str,
        name: str,
        description: str | None,
        aggregation: str,
        field: str | None,
    ) -> MetricView:
        try:
            clean_code = validate_code(code)
        except ValueError as error:
            raise _invalid("/code", str(error)) from None
        try:
            clean_name = validate_name(name)
        except ValueError as error:
            raise _invalid("/name", str(error)) from None
        try:
            clean_description = validate_description(description)
        except ValueError as error:
            raise _invalid("/description", str(error)) from None
        try:
            parsed, clean_field = validate_definition(aggregation, field)
        except ValueError as error:
            pointer = "/field" if "field" in str(error) else "/aggregation"
            raise _invalid(pointer, str(error)) from None
        now = self._clock.now()
        inserted = await repository.insert_metric(
            self._uow.session,
            metric_id=new_uuid(),
            project_id=scope.project_id,
            code=clean_code,
            name=clean_name,
            description=clean_description,
            aggregation=parsed,
            field=clean_field,
            now=now,
        )
        if not inserted:
            raise MetricCodeTaken(f"The project already has a metric with code {clean_code}.")
        await self._record(
            scope,
            "metric.created",
            clean_code,
            {"aggregation": parsed.value, "field": clean_field, "name": clean_name},
        )
        return _metric_view(await self._require_metric(scope.project_id, clean_code))

    async def _require_metric(self, project_id: UUID, code: str) -> BillableMetric:
        metric = await repository.get_metric(self._uow.session, project_id, code)
        if metric is None:
            raise NotFound("Metric not found.")
        return metric

    async def get_metric(self, project_id: UUID, code: str) -> MetricView:
        return _metric_view(await self._require_metric(project_id, code))

    async def list_metrics(
        self, project_id: UUID, params: PageParams, *, status: str | None
    ) -> Page[MetricView]:
        try:
            wanted = None if status is None else MetricStatus(status)
        except ValueError:
            raise _invalid("/status", "status must be active or archived") from None
        rows = await repository.list_metrics(
            self._uow.session,
            project_id,
            status=wanted,
            cursor=params.decoded_cursor(),
            limit=params.limit + 1,
        )
        return build_page(
            rows,
            params.limit,
            _metric_view,
            lambda metric: Cursor(created_at=metric.created_at, id=metric.id),
        )

    async def archive_metric(self, scope: ProjectScope, code: str) -> MetricView:
        """Archive (final: no more usage is accepted, history stays readable); idempotent."""
        metric = await self._require_metric(scope.project_id, code)
        if not metric.is_active:
            return _metric_view(metric)
        archived = await repository.archive_metric(
            self._uow.session, scope.project_id, code, self._clock.now()
        )
        if archived is None:  # archived concurrently since the read above
            return _metric_view(await self._require_metric(scope.project_id, code))
        await self._record(scope, "metric.archived", code, {"status": "archived"})
        return _metric_view(archived)

    # -- ingestion -------------------------------------------------------------------------

    async def ingest(self, project_id: UUID, item: UsageItem) -> Ingested:
        """Record one event; raises ``ValidationFailed`` (422) or ``UsageTransactionConflict``."""
        return await self._ingest(project_id, item, self._clock.now())

    async def ingest_batch(self, project_id: UUID, items: Sequence[UsageItem]) -> list[ItemResult]:
        """Per-item semantics: each event is decided on its own (see ``ingest``); a rejected event
        writes nothing and does not stop the others. Events of one batch are processed in order, so
        a transaction id repeated inside the batch is compared with the first occurrence."""
        now = self._clock.now()
        results: list[ItemResult] = []
        for item in items:
            try:
                results.append(ItemResult(ingested=await self._ingest(project_id, item, now)))
            except (ValidationFailed, UsageTransactionConflict) as error:
                results.append(ItemResult(error=error))
        return results

    async def _ingest(self, project_id: UUID, item: UsageItem, now: datetime) -> Ingested:
        session = self._uow.session
        try:
            transaction_id = validate_transaction_id(item.transaction_id)
        except ValueError as error:
            raise _invalid("/transaction_id", str(error)) from None
        try:
            properties = validate_properties(item.properties)
        except ValueError as error:
            raise _invalid("/properties", str(error)) from None
        timestamp = item.timestamp
        if timestamp is not None and (timestamp.tzinfo is None or timestamp.utcoffset() is None):
            raise _invalid("/timestamp", "timestamp must include a timezone offset")
        if item.customer_id is None and item.customer_external_ref is None:
            raise _invalid(
                "/customer_id", "customer_id or customer_external_ref is required", "required"
            )
        customer_pointer = "/customer_id" if item.customer_id else "/customer_external_ref"
        customer = await resolve_customer_identity(
            session,
            project_id=project_id,
            customer_id=item.customer_id,
            external_ref=item.customer_external_ref,
        )
        if customer is None:
            raise _invalid(customer_pointer, "customer not found", "not_found")

        existing = await repository.get_event(session, project_id, transaction_id)
        if existing is not None:
            return self._duplicate(existing, item.metric, customer, properties, timestamp)

        metric = await repository.get_metric(session, project_id, item.metric)
        if metric is None:
            raise _invalid("/metric", f"metric {item.metric} not found", "not_found")
        if not metric.is_active:
            raise _invalid("/metric", f"metric {item.metric} is archived", "archived")
        try:
            measure = measure_of(metric, properties)
        except ValueError as error:
            raise _invalid("/properties", str(error)) from None
        try:
            occurred_at = check_window(timestamp or now, now)
        except ValueError as error:
            raise _invalid("/timestamp", str(error), "out_of_window") from None

        event = UsageEvent(
            id=new_uuid(),
            project_id=project_id,
            customer_id=customer,
            metric_code=metric.code,
            transaction_id=transaction_id,
            occurred_at=occurred_at,
            properties=properties,
            value=measure.value,
            unique_value=measure.unique_value,
            ingested_at=now,
        )
        if not await repository.insert_event(session, event):
            # A concurrent request recorded this transaction id first: decide as for a retry.
            winner = await repository.get_event(session, project_id, transaction_id)
            if winner is None:  # pragma: no cover - rows are never deleted
                raise UsageTransactionConflict("The transaction id is being recorded; retry.")
            return self._duplicate(winner, item.metric, customer, properties, timestamp)
        await self._publish(event)
        return Ingested(usage=_usage_view(event), created=True)

    @staticmethod
    def _duplicate(
        stored: UsageEvent,
        metric: str,
        customer: UUID,
        properties: dict[str, Any],
        timestamp: datetime | None,
    ) -> Ingested:
        if not same_payload(
            stored,
            metric_code=metric,
            customer_id=customer,
            properties=properties,
            timestamp=timestamp,
        ):
            raise UsageTransactionConflict(
                f"Transaction {stored.transaction_id} was already recorded with a different "
                "metric, customer, properties or timestamp."
            )
        return Ingested(usage=_usage_view(stored), created=False)

    async def _publish(self, event: UsageEvent) -> None:
        await self._publisher.publish(
            self._uow,
            EventDraft(
                type=metering_events.USAGE_RECORDED.name,
                data=metering_events.UsageRecordedPayload(
                    transaction_id=event.transaction_id,
                    customer_id=format_id(IdPrefix.CUSTOMER, event.customer_id),
                    metric=event.metric_code,
                    timestamp=event.occurred_at,
                    value=None if event.value is None else plain_decimal(event.value),
                ),
                project_id=event.project_id,
                subject=event.transaction_id,
            ),
        )

    # -- aggregation -----------------------------------------------------------------------

    async def summary(
        self,
        project_id: UUID,
        *,
        customer_id: str,
        metric_code: str,
        start: datetime,
        end: datetime,
    ) -> UsageSummary:
        """The metric's value for one customer over ``[start, end)`` (archived metrics included)."""
        for pointer, moment in (("/from", start), ("/to", end)):
            if moment.tzinfo is None or moment.utcoffset() is None:
                raise _invalid(pointer, "must include a timezone offset")
        start, end = start.astimezone(UTC), end.astimezone(UTC)
        if start >= end:
            raise _invalid("/from", "from must be before to")
        customer = await resolve_customer_identity(
            self._uow.session, project_id=project_id, customer_id=customer_id, external_ref=None
        )
        if customer is None:
            raise NotFound("Customer not found.")
        metric = await self._require_metric(project_id, metric_code)
        value, events = await repository.aggregate(
            self._uow.session,
            project_id=project_id,
            customer_id=customer,
            metric=metric,
            start=start,
            end=end,
        )
        return UsageSummary(
            customer_id=customer_id,
            metric=metric.code,
            aggregation=metric.aggregation.value,
            start=start,
            end=end,
            value=None if value is None else plain_decimal(value),
            events=events,
        )
