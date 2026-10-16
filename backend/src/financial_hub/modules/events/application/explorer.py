"""Event Explorer use cases (W3.3 / PRO-39): read-model queries behind the explorer API.

Everything runs on the caller's Unit of Work (one read-only transaction per request). Bounds per
spec: the causality tree walks at most ``MAX_CAUSALITY_DEPTH`` ancestors up and at most
``MAX_CAUSALITY_NODES`` events in the whole tree; list pages default to 50 and cap at 200
(``shared.pagination.validate_limit``). All identifier-shaped inputs are validated here so the api
layer stays a pure view over the returned dataclasses.
"""

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from financial_hub.modules.events.domain import (
    Event,
    EventCursor,
    is_valid_type_filter,
    truncate_error,
)
from financial_hub.modules.events.infrastructure import explorer_queries
from financial_hub.platform.db import UnitOfWork
from financial_hub.shared.errors import FieldError, NotFound, ValidationFailed
from financial_hub.shared.pagination import validate_limit

MAX_CAUSALITY_DEPTH: int = 20
MAX_CAUSALITY_NODES: int = 500
LAST_ERROR_LIMIT: int = 500


@dataclass(frozen=True, slots=True)
class EventView:
    event: Event
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class DeliveryView:
    id: UUID
    subscription_id: UUID
    kind: str
    handler: str | None
    status: str
    attempts: int
    next_attempt_at: datetime
    last_error: str | None


@dataclass(frozen=True, slots=True)
class JournalEntryView:
    id: UUID
    project_id: UUID
    status: str
    recipe: str
    effective_at: datetime
    reverses_entry_id: UUID | None
    correlation_id: UUID | None
    causation_id: UUID | None


@dataclass(frozen=True, slots=True)
class EventPageView:
    events: list[EventView]
    next_cursor: EventCursor | None
    has_more: bool


@dataclass(frozen=True, slots=True)
class EventDetailView:
    event: EventView
    deliveries: list[DeliveryView]


@dataclass(frozen=True, slots=True)
class CausalityNodeView:
    event: EventView
    children: list[CausalityNodeView]


@dataclass(frozen=True, slots=True)
class CausalityView:
    root: EventView
    ancestors: list[EventView]
    descendants: list[CausalityNodeView]
    journal_entries: list[JournalEntryView]
    node_count: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class CorrelationView:
    correlation_id: UUID
    events: list[EventView]
    journal_entries: list[JournalEntryView]


def _record_to_view(record: explorer_queries.EventRecord) -> EventView:
    return EventView(record.event, record.recorded_at)


def _delivery_to_view(row: explorer_queries.DeliveryRecord) -> DeliveryView:
    return DeliveryView(
        id=row.id,
        subscription_id=row.subscription_id,
        kind=row.kind,
        handler=row.handler,
        status=row.status,
        attempts=row.attempts,
        next_attempt_at=row.next_attempt_at,
        last_error=(
            None if row.last_error is None else truncate_error(row.last_error, LAST_ERROR_LIMIT)
        ),
    )


def _entry_to_view(row: explorer_queries.JournalEntryRecord) -> JournalEntryView:
    return JournalEntryView(
        id=row.id,
        project_id=row.project_id,
        status=row.status,
        recipe=row.recipe,
        effective_at=row.effective_at,
        reverses_entry_id=row.reverses_entry_id,
        correlation_id=row.correlation_id,
        causation_id=row.causation_id,
    )


def _require_type_filter(value: str) -> str:
    if not is_valid_type_filter(value):
        raise ValidationFailed(
            "invalid event type filter",
            errors=[
                FieldError(
                    pointer="/type", detail=f"{value!r} is not a valid filter", code="invalid_type"
                )
            ],
        )
    return value


def _require_uuid(value: str, pointer: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise ValidationFailed(
            "invalid UUID",
            errors=[
                FieldError(
                    pointer=pointer, detail=f"{value!r} is not a valid UUID", code="invalid_uuid"
                )
            ],
        ) from None


def _require_aware(value: datetime, pointer: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationFailed(
            "timestamps must be timezone-aware",
            errors=[FieldError(pointer=pointer, detail="must be timezone-aware", code="timezone")],
        )
    return value


# --- list --------------------------------------------------------------------------------------


async def list_events(
    uow: UnitOfWork,
    *,
    project_id: UUID,
    cursor: EventCursor | str | None = None,
    event_type: str | None = None,
    correlation_id: str | None = None,
    subject: str | None = None,
    occurred_from: datetime | None = None,
    occurred_to: datetime | None = None,
    limit: int | None = None,
) -> EventPageView:
    """Newest-first page of the project's events with cursor pagination on ``(txid, id)``."""
    position = EventCursor.decode(cursor) if isinstance(cursor, str) else cursor
    page_size = validate_limit(limit)
    if event_type is not None:
        _require_type_filter(event_type)
    corr = _require_uuid(correlation_id, "/correlation_id") if correlation_id is not None else None
    if occurred_from is not None:
        _require_aware(occurred_from, "/occurred_from")
    if occurred_to is not None:
        _require_aware(occurred_to, "/occurred_to")
    records = await explorer_queries.list_events(
        uow.session,
        project_id=project_id,
        cursor=position,
        event_type=event_type,
        correlation_id=corr,
        subject=subject,
        occurred_from=occurred_from,
        occurred_to=occurred_to,
        limit=page_size + 1,
    )
    has_more = len(records) > page_size
    visible = records[:page_size]
    next_cursor = visible[-1].event.cursor if has_more and visible else None
    return EventPageView(
        events=[_record_to_view(record) for record in visible],
        next_cursor=next_cursor,
        has_more=has_more,
    )


# --- reads of one event ------------------------------------------------------------------------


async def event_details(uow: UnitOfWork, *, project_id: UUID, event_id: UUID) -> EventDetailView:
    record = await explorer_queries.get_event(uow.session, project_id=project_id, event_id=event_id)
    if record is None:
        raise NotFound("event not found")
    deliveries = await explorer_queries.list_deliveries(
        uow.session, project_id=project_id, event_id=event_id
    )
    return EventDetailView(
        event=_record_to_view(record), deliveries=[_delivery_to_view(row) for row in deliveries]
    )


async def list_deliveries(
    uow: UnitOfWork, *, project_id: UUID, event_id: UUID
) -> list[DeliveryView]:
    record = await explorer_queries.get_event(uow.session, project_id=project_id, event_id=event_id)
    if record is None:
        raise NotFound("event not found")
    rows = await explorer_queries.list_deliveries(
        uow.session, project_id=project_id, event_id=event_id
    )
    return [_delivery_to_view(row) for row in rows]


# --- causality ---------------------------------------------------------------------------------


async def _descendants(
    session: AsyncSession,
    *,
    project_id: UUID,
    root_id: UUID,
    root_record: explorer_queries.EventRecord,
    budget: int,
) -> tuple[list[CausalityNodeView], list[EventView], bool]:
    """Children of ``root`` as a tree (BFS), bounded by depth and total node budget.

    Returns ``(tree, flat_views, truncated)``; the flat list excludes the root. ``budget`` is how
    many descendant events may be included; cycles and diamonds include each event once (the
    first parent found wins).
    """
    records: dict[UUID, explorer_queries.EventRecord] = {root_id: root_record}
    children_of: dict[UUID, list[UUID]] = {root_id: []}
    queue: deque[tuple[UUID, int]] = deque([(root_id, 1)])
    truncated = False
    while queue:
        parent_id, depth = queue.popleft()
        if depth > MAX_CAUSALITY_DEPTH:
            truncated = True
            continue
        for record in await explorer_queries.children_of(
            session, project_id=project_id, parent_id=parent_id
        ):
            child_id = record.event.id
            if child_id in records:
                continue
            if len(records) - 1 >= budget:
                truncated = True
                continue
            records[child_id] = record
            children_of.setdefault(parent_id, []).append(child_id)
            children_of[child_id] = []
            queue.append((child_id, depth + 1))

    def build(parent_id: UUID) -> list[CausalityNodeView]:
        return [
            CausalityNodeView(_record_to_view(records[child_id]), build(child_id))
            for child_id in children_of.get(parent_id, ())
        ]

    flat = [_record_to_view(record) for key, record in records.items() if key != root_id]
    return build(root_id), flat, truncated


async def causality(uow: UnitOfWork, *, project_id: UUID, event_id: UUID) -> CausalityView:
    """Causation tree of ``event_id``: ancestors up to the root, descendants down (bounded)."""
    root = await explorer_queries.get_event(uow.session, project_id=project_id, event_id=event_id)
    if root is None:
        raise NotFound("event not found")
    seen: set[UUID] = {event_id}
    ancestors: list[EventView] = []
    current = root
    truncated = False
    while current.event.causation_id is not None:
        if len(ancestors) >= MAX_CAUSALITY_DEPTH:
            truncated = True
            break
        parent_id = current.event.causation_id
        if parent_id in seen:
            break  # malformed cycle: never walk in circles
        parent = await explorer_queries.get_event(
            uow.session, project_id=project_id, event_id=parent_id
        )
        if parent is None:
            break
        seen.add(parent_id)
        ancestors.append(_record_to_view(parent))
        current = parent
    budget = max(0, MAX_CAUSALITY_NODES - 1 - len(ancestors))
    descendants, descendant_views, descendants_truncated = await _descendants(
        uow.session, project_id=project_id, root_id=event_id, root_record=root, budget=budget
    )
    truncated = truncated or descendants_truncated
    tree_views = [_record_to_view(root), *ancestors, *descendant_views]
    entries = await explorer_queries.linked_journal_entries(
        uow.session,
        project_id=project_id,
        causation_ids=[view.event.id for view in tree_views],
        correlation_ids=[
            view.event.correlation_id
            for view in tree_views
            if view.event.correlation_id is not None
        ],
    )
    return CausalityView(
        root=_record_to_view(root),
        ancestors=ancestors,
        descendants=descendants,
        journal_entries=[_entry_to_view(entry) for entry in entries],
        node_count=len(tree_views),
        truncated=truncated,
    )


# --- correlation chain -------------------------------------------------------------------------


async def correlation_chain(
    uow: UnitOfWork, *, project_id: UUID, correlation_id: UUID
) -> CorrelationView:
    """The full chain of ``correlation_id``: events in commit order plus linked journal entries."""
    records = await explorer_queries.correlation_chain(
        uow.session, project_id=project_id, correlation_id=correlation_id
    )
    entries = await explorer_queries.linked_journal_entries(
        uow.session,
        project_id=project_id,
        causation_ids=[record.event.id for record in records],
        correlation_ids=[correlation_id],
    )
    return CorrelationView(
        correlation_id=correlation_id,
        events=[_record_to_view(record) for record in records],
        journal_entries=[_entry_to_view(entry) for entry in entries],
    )
