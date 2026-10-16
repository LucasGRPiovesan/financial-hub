"""Metering: billable metrics, usage ingestion and the usage summary (docs/METERING.md).

- ``/v1/projects/{project_id}/metrics``: the operator console manages metrics (``metering:manage``)
  and reads them (``metering:read``). A metric is addressed by its ``code``.
- ``/v1/usage`` and ``/v1/usage/batch``: a project **API key** with the ``usage:write`` scope
  records usage; the project is the key's. Operators do not ingest. Idempotent by
  ``transaction_id`` (no ``Idempotency-Key`` header): the same payload answers 200 with the
  original event, a different payload 409.
- ``/v1/projects/{project_id}/usage/summary``: aggregation of one customer's usage for one metric
  over ``[from, to)`` (``metering:read``).
"""

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import BaseModel, Field

from financial_hub.modules.audit.application import AuditContext, request_audit_context
from financial_hub.modules.identity.application.api import Permission
from financial_hub.modules.metering.application.service import (
    BATCH_LIMIT,
    Ingested,
    ItemResult,
    MeteringService,
    MetricView,
    UsageItem,
    UsageSummary,
    UsageView,
)
from financial_hub.platform.db import UnitOfWork
from financial_hub.platform.http.auth import Principal, ProjectScope, authenticated, require
from financial_hub.platform.http.problems import (
    Problem,
    ProblemFieldError,
    problem_type,
    status_for,
)
from financial_hub.shared.errors import DomainError, Forbidden
from financial_hub.shared.pagination import Page, PageParams

# The scheme name of project API keys (``projects.application.api_keys.API_KEY_PRINCIPAL``).
API_KEY_KIND = "api_key"

metrics_router = APIRouter(prefix="/v1/projects/{project_id}", tags=["metering"])
usage_router = APIRouter(prefix="/v1/usage", tags=["metering"])


class MetricOut(BaseModel):
    code: str
    name: str
    description: str | None
    aggregation: str = Field(description="`sum`, `count`, `max` or `unique_count`.")
    field: str | None = Field(description="The event property it reads; null for `count`.")
    status: str = Field(description="`active` or `archived`.")
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None

    @classmethod
    def of(cls, view: MetricView) -> MetricOut:
        return cls(
            code=view.code,
            name=view.name,
            description=view.description,
            aggregation=view.aggregation,
            field=view.field,
            status=view.status,
            created_at=view.created_at,
            updated_at=view.updated_at,
            archived_at=view.archived_at,
        )


class CreateMetricRequest(BaseModel):
    code: str = Field(max_length=100, description="Lowercase letters, digits and `_`, unique.")
    name: str = Field(max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    aggregation: str = Field(max_length=32, description="`sum`, `count`, `max` or `unique_count`.")
    field: str | None = Field(
        default=None,
        max_length=100,
        description="The event property to read; required unless `count`. Immutable.",
    )


class UsageOut(BaseModel):
    transaction_id: str
    customer_id: str
    metric: str
    timestamp: datetime
    properties: dict[str, Any]
    value: str | None = Field(
        description="Exact decimal measure of a `sum`/`max` metric; null otherwise."
    )
    ingested_at: datetime

    @classmethod
    def of(cls, view: UsageView) -> UsageOut:
        return cls(
            transaction_id=view.transaction_id,
            customer_id=view.customer_id,
            metric=view.metric,
            timestamp=view.timestamp,
            properties=view.properties,
            value=view.value,
            ingested_at=view.ingested_at,
        )


class UsageRequest(BaseModel):
    transaction_id: str = Field(
        max_length=200, description="Your unique id of this event (idempotency key), per project."
    )
    metric: str = Field(max_length=100, description="Code of an active metric.")
    customer_id: str | None = Field(default=None, max_length=64, description="`cus_...` id.")
    customer_external_ref: str | None = Field(
        default=None, max_length=128, description="The project's own customer reference."
    )
    timestamp: datetime | None = Field(
        default=None,
        description=(
            "When the usage happened (with timezone offset). Within the last 30 days and at most "
            "5 minutes ahead. Defaults to the time of receipt."
        ),
    )
    properties: dict[str, Any] = Field(
        default_factory=dict[str, Any],
        description="Event properties; the metric's `field` is read from here.",
    )

    def item(self) -> UsageItem:
        return UsageItem(
            transaction_id=self.transaction_id,
            metric=self.metric,
            properties=self.properties,
            timestamp=self.timestamp,
            customer_id=self.customer_id,
            customer_external_ref=self.customer_external_ref,
        )


class BatchRequest(BaseModel):
    events: list[UsageRequest] = Field(min_length=1, max_length=BATCH_LIMIT)


class BatchItemOut(BaseModel):
    index: int
    status: Literal["created", "duplicate", "rejected"]
    usage: UsageOut | None = Field(
        description="The recorded (or original) event; null if rejected."
    )
    error: Problem | None = Field(description="Why the event was rejected; null otherwise.")


class BatchOut(BaseModel):
    """Always 200: look at each result. Events are independent (per-item semantics)."""

    results: list[BatchItemOut]
    created: int
    duplicates: int
    rejected: int


class SummaryOut(BaseModel):
    customer_id: str
    metric: str
    aggregation: str
    start: datetime = Field(description="Inclusive start of the range (UTC).")
    end: datetime = Field(description="Exclusive end of the range (UTC).")
    value: str | None = Field(
        description="Exact decimal as a string; `max` of no events is null, the rest are 0."
    )
    events: int = Field(description="Events in the range.")

    @classmethod
    def of(cls, summary: UsageSummary) -> SummaryOut:
        return cls(
            customer_id=summary.customer_id,
            metric=summary.metric,
            aggregation=summary.aggregation,
            start=summary.start,
            end=summary.end,
            value=summary.value,
            events=summary.events,
        )


def _uow(request: Request) -> UnitOfWork:
    return UnitOfWork(request.app.state.session_factory)


def _service(
    request: Request, uow: UnitOfWork, audit: AuditContext | None = None
) -> MeteringService:
    return MeteringService(uow, clock=request.app.state.clock, audit=audit)


ReadMetering = Annotated[ProjectScope, require(Permission.METERING_READ)]
ManageMetering = Annotated[ProjectScope, require(Permission.METERING_MANAGE)]
AuditCtx = Annotated[AuditContext, Depends(request_audit_context)]


# -- metrics (operator console) ---------------------------------------------------------------


@metrics_router.post(
    "/metrics",
    status_code=status.HTTP_201_CREATED,
    responses={status.HTTP_409_CONFLICT: {"model": Problem, "description": "Code in use."}},
)
async def create_metric(
    body: CreateMetricRequest, scope: ManageMetering, request: Request, audit: AuditCtx
) -> MetricOut:
    """Define how usage is aggregated. The definition cannot change afterwards, only archive."""
    async with _uow(request) as uow:
        view = await _service(request, uow, audit).create_metric(
            scope,
            code=body.code,
            name=body.name,
            description=body.description,
            aggregation=body.aggregation,
            field=body.field,
        )
    return MetricOut.of(view)


class ListMetricsQuery(PageParams):
    status: str | None = Field(default=None, max_length=16, description="`active` or `archived`.")


@metrics_router.get("/metrics")
async def list_metrics(
    scope: ReadMetering, params: Annotated[ListMetricsQuery, Query()], request: Request
) -> Page[MetricOut]:
    async with _uow(request) as uow:
        page = await _service(request, uow).list_metrics(
            scope.project_id, params, status=params.status
        )
    return Page[MetricOut](
        data=[MetricOut.of(view) for view in page.data],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


@metrics_router.get("/metrics/{code}")
async def get_metric(code: str, scope: ReadMetering, request: Request) -> MetricOut:
    async with _uow(request) as uow:
        view = await _service(request, uow).get_metric(scope.project_id, code)
    return MetricOut.of(view)


@metrics_router.post("/metrics/{code}/archive")
async def archive_metric(
    code: str, scope: ManageMetering, request: Request, audit: AuditCtx
) -> MetricOut:
    """Stop accepting usage for the metric (final). Recorded usage stays readable. Idempotent."""
    async with _uow(request) as uow:
        view = await _service(request, uow, audit).archive_metric(scope, code)
    return MetricOut.of(view)


@metrics_router.get("/usage/summary")
async def usage_summary(
    scope: ReadMetering,
    request: Request,
    customer_id: Annotated[str, Query(max_length=64, description="`cus_...` id.")],
    metric: Annotated[str, Query(max_length=100, description="Metric code.")],
    start: Annotated[datetime, Query(alias="from", description="Inclusive, with timezone.")],
    end: Annotated[datetime, Query(alias="to", description="Exclusive, with timezone.")],
) -> SummaryOut:
    """The metric's value for the customer over ``[from, to)``, computed by the database."""
    async with _uow(request) as uow:
        summary = await _service(request, uow).summary(
            scope.project_id,
            customer_id=customer_id,
            metric_code=metric,
            start=start,
            end=end,
        )
    return SummaryOut.of(summary)


# -- ingestion (project API key) --------------------------------------------------------------


def _ingestion_scope(principal: Principal) -> ProjectScope:
    """The key's project scope, if the caller is an API key allowed to record usage."""
    if principal.kind != API_KEY_KIND or len(principal.project_ids) != 1:
        raise Forbidden("Usage is recorded with a project API key.")
    (project_id,) = principal.project_ids
    scope = principal.scope_for(project_id)
    if scope is None or not scope.allows(Permission.USAGE_WRITE):
        raise Forbidden(f"Missing permission {Permission.USAGE_WRITE}.")
    return scope


Caller = Annotated[Principal, authenticated()]


@usage_router.post(
    "",
    responses={
        status.HTTP_201_CREATED: {"model": UsageOut, "description": "Recorded."},
        status.HTTP_409_CONFLICT: {
            "model": Problem,
            "description": "`transaction_id` already recorded with a different payload.",
        },
    },
)
async def record_usage(
    body: UsageRequest, principal: Caller, request: Request, response: Response
) -> UsageOut:
    """Record one usage event. A retry of the same event answers 200 with the original."""
    scope = _ingestion_scope(principal)
    async with _uow(request) as uow:
        result = await _service(request, uow).ingest(scope.project_id, body.item())
    if result.created:
        response.status_code = status.HTTP_201_CREATED
    return UsageOut.of(result.usage)


def _problem(error: DomainError, index: int) -> Problem:
    return Problem(
        type=problem_type(error.slug),
        title=error.title,
        status=status_for(error),
        detail=error.detail,
        errors=[
            ProblemFieldError(
                pointer=f"/events/{index}{field.pointer}", detail=field.detail, code=field.code
            )
            for field in error.errors
        ]
        or None,
    )


def _item_out(index: int, result: ItemResult) -> BatchItemOut:
    ingested: Ingested | None = result.ingested
    if ingested is not None:
        return BatchItemOut(
            index=index,
            status="created" if ingested.created else "duplicate",
            usage=UsageOut.of(ingested.usage),
            error=None,
        )
    error = result.error
    return BatchItemOut(
        index=index,
        status="rejected",
        usage=None,
        error=None if error is None else _problem(error, index),
    )


@usage_router.post("/batch")
async def record_usage_batch(body: BatchRequest, principal: Caller, request: Request) -> BatchOut:
    """Record up to 100 events. **Per-item**: each event is accepted or rejected on its own (a
    bad one writes nothing and does not stop the others), the response is always 200 and lists
    every outcome in order. Retry the whole batch safely: accepted events come back as
    `duplicate`."""
    scope = _ingestion_scope(principal)
    async with _uow(request) as uow:
        results = await _service(request, uow).ingest_batch(
            scope.project_id, [event.item() for event in body.events]
        )
    items = [_item_out(index, result) for index, result in enumerate(results)]
    return BatchOut(
        results=items,
        created=sum(1 for item in items if item.status == "created"),
        duplicates=sum(1 for item in items if item.status == "duplicate"),
        rejected=sum(1 for item in items if item.status == "rejected"),
    )
