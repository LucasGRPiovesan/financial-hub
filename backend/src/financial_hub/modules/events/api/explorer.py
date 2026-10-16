"""``/v1/projects/{project_id}/events``: the Event Explorer (W3.3 / PRO-39), read-only.

Every route is GET and requires ``events:read`` (``require()`` turns non-members into 404,
ADR-0011).
Response ids use the TypeID prefixes of ADR-0001 (``evt_``, ``dlv_``, ``je_``, ``prj_``);
``causation_id`` is a parent event id and ``correlation_id`` has no entity prefix, so the former is
rendered ``evt_``-prefixed and the latter as a plain UUID string (same convention as the SSE
stream). No route writes anything: each handler opens one read-only Unit of Work.
"""

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from financial_hub.modules.events.application.explorer import (
    CausalityNodeView,
    CausalityView,
    CorrelationView,
    DeliveryView,
    EventDetailView,
    EventPageView,
    EventView,
    JournalEntryView,
    causality,
    correlation_chain,
    event_details,
    list_deliveries,
    list_events,
)
from financial_hub.modules.identity.application.api import Permission
from financial_hub.platform.db import UnitOfWork
from financial_hub.platform.http.auth import ProjectScope, require
from financial_hub.shared.errors import NotFound
from financial_hub.shared.ids import IdPrefix, InvalidIdError, format_id, parse_id

router = APIRouter(prefix="/v1/projects", tags=["events"])

CurrentScope = Annotated[ProjectScope, require(Permission.EVENTS_READ)]


def _uow(request: Request) -> UnitOfWork:
    return UnitOfWork(request.app.state.session_factory)


def _event_id(value: str) -> UUID:
    try:
        return parse_id(value, IdPrefix.EVENT)
    except InvalidIdError:
        raise NotFound("event not found.") from None


def _correlation_id(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise NotFound("correlation not found.") from None


# --- response models ---------------------------------------------------------------------------


class EventOut(BaseModel):
    id: str
    type: str
    version: int
    project_id: str
    subject: str | None
    occurred_at: datetime
    recorded_at: datetime
    correlation_id: str | None
    causation_id: str | None
    traceparent: str | None
    txid: int
    data: dict[str, Any]

    @classmethod
    def of(cls, view: EventView) -> EventOut:
        event = view.event
        return cls(
            id=format_id(IdPrefix.EVENT, event.id),
            type=event.type,
            version=event.version,
            project_id=format_id(IdPrefix.PROJECT, event.project_id),
            subject=event.subject,
            occurred_at=event.occurred_at,
            recorded_at=view.recorded_at,
            correlation_id=None if event.correlation_id is None else str(event.correlation_id),
            causation_id=(
                None
                if event.causation_id is None
                else format_id(IdPrefix.EVENT, event.causation_id)
            ),
            traceparent=event.traceparent,
            txid=event.txid,
            data=dict(event.data),
        )


class DeliveryOut(BaseModel):
    id: str
    subscription_id: str
    kind: str
    handler: str | None
    status: str
    attempts: int
    next_attempt_at: datetime
    last_error: str | None

    @classmethod
    def of(cls, view: DeliveryView) -> DeliveryOut:
        return cls(
            id=format_id(IdPrefix.DELIVERY, view.id),
            subscription_id=str(view.subscription_id),
            kind=view.kind,
            handler=view.handler,
            status=view.status,
            attempts=view.attempts,
            next_attempt_at=view.next_attempt_at,
            last_error=view.last_error,
        )


class JournalEntryOut(BaseModel):
    id: str
    status: str
    recipe: str
    effective_at: datetime
    reverses_entry_id: str | None
    correlation_id: str | None
    causation_id: str | None

    @classmethod
    def of(cls, view: JournalEntryView) -> JournalEntryOut:
        return cls(
            id=format_id(IdPrefix.JOURNAL_ENTRY, view.id),
            status=view.status,
            recipe=view.recipe,
            effective_at=view.effective_at,
            reverses_entry_id=(
                None
                if view.reverses_entry_id is None
                else format_id(IdPrefix.JOURNAL_ENTRY, view.reverses_entry_id)
            ),
            correlation_id=None if view.correlation_id is None else str(view.correlation_id),
            causation_id=(
                None if view.causation_id is None else format_id(IdPrefix.EVENT, view.causation_id)
            ),
        )


class EventListOut(BaseModel):
    data: list[EventOut]
    next_cursor: str | None
    has_more: bool

    @classmethod
    def of(cls, view: EventPageView) -> EventListOut:
        return cls(
            data=[EventOut.of(item) for item in view.events],
            next_cursor=view.next_cursor.encode() if view.next_cursor is not None else None,
            has_more=view.has_more,
        )


class EventDetailOut(BaseModel):
    event: EventOut
    deliveries: list[DeliveryOut]

    @classmethod
    def of(cls, view: EventDetailView) -> EventDetailOut:
        return cls(
            event=EventOut.of(view.event),
            deliveries=[DeliveryOut.of(item) for item in view.deliveries],
        )


class CausalityNodeOut(BaseModel):
    event: EventOut
    children: list[CausalityNodeOut]

    @classmethod
    def of(cls, view: CausalityNodeView) -> CausalityNodeOut:
        return cls(
            event=EventOut.of(view.event),
            children=[CausalityNodeOut.of(c) for c in view.children],
        )


class CausalityOut(BaseModel):
    root: EventOut
    ancestors: list[EventOut]
    descendants: list[CausalityNodeOut]
    journal_entries: list[JournalEntryOut]
    node_count: int
    truncated: bool

    @classmethod
    def of(cls, view: CausalityView) -> CausalityOut:
        return cls(
            root=EventOut.of(view.root),
            ancestors=[EventOut.of(item) for item in view.ancestors],
            descendants=[CausalityNodeOut.of(node) for node in view.descendants],
            journal_entries=[JournalEntryOut.of(item) for item in view.journal_entries],
            node_count=view.node_count,
            truncated=view.truncated,
        )


class CorrelationOut(BaseModel):
    correlation_id: str
    events: list[EventOut]
    journal_entries: list[JournalEntryOut]

    @classmethod
    def of(cls, view: CorrelationView) -> CorrelationOut:
        return cls(
            correlation_id=str(view.correlation_id),
            events=[EventOut.of(item) for item in view.events],
            journal_entries=[JournalEntryOut.of(item) for item in view.journal_entries],
        )


# --- routes ------------------------------------------------------------------------------------


@router.get("/{project_id}/events")
async def list_project_events(
    scope: CurrentScope,
    request: Request,
    type: Annotated[str | None, Query()] = None,
    correlation_id: Annotated[str | None, Query()] = None,
    subject: Annotated[str | None, Query()] = None,
    occurred_from: Annotated[datetime | None, Query()] = None,
    occurred_to: Annotated[datetime | None, Query()] = None,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int | None, Query()] = None,
) -> EventListOut:
    async with _uow(request) as uow:
        page = await list_events(
            uow,
            project_id=scope.project_id,
            cursor=cursor,
            event_type=type,
            correlation_id=correlation_id,
            subject=subject,
            occurred_from=occurred_from,
            occurred_to=occurred_to,
            limit=limit,
        )
    return EventListOut.of(page)


@router.get("/{project_id}/events/{event_id}")
async def get_project_event(scope: CurrentScope, event_id: str, request: Request) -> EventDetailOut:
    async with _uow(request) as uow:
        detail = await event_details(uow, project_id=scope.project_id, event_id=_event_id(event_id))
    return EventDetailOut.of(detail)


@router.get("/{project_id}/events/{event_id}/causality")
async def get_event_causality(scope: CurrentScope, event_id: str, request: Request) -> CausalityOut:
    async with _uow(request) as uow:
        tree = await causality(uow, project_id=scope.project_id, event_id=_event_id(event_id))
    return CausalityOut.of(tree)


@router.get("/{project_id}/events/{event_id}/deliveries")
async def get_event_deliveries(
    scope: CurrentScope, event_id: str, request: Request
) -> list[DeliveryOut]:
    async with _uow(request) as uow:
        rows = await list_deliveries(uow, project_id=scope.project_id, event_id=_event_id(event_id))
    return [DeliveryOut.of(row) for row in rows]


@router.get("/{project_id}/correlations/{correlation_id}")
async def get_correlation_chain(
    scope: CurrentScope, correlation_id: str, request: Request
) -> CorrelationOut:
    corr = _correlation_id(correlation_id)
    async with _uow(request) as uow:
        chain = await correlation_chain(uow, project_id=scope.project_id, correlation_id=corr)
    return CorrelationOut.of(chain)
