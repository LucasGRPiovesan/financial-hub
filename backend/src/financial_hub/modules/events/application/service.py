"""Event log commands and queries other modules (and the api) call with their Unit of Work."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from financial_hub.modules.events.application.handlers import HandlerRegistry
from financial_hub.modules.events.domain import (
    Event,
    EventCursor,
    is_valid_type_filter,
    require_name,
)
from financial_hub.modules.events.infrastructure import repository
from financial_hub.modules.events.registry import EventRegistry, ScheduleDuePayload
from financial_hub.modules.events.registry import registry as canonical_registry
from financial_hub.platform.db import UnitOfWork
from financial_hub.shared.clock import Clock
from financial_hub.shared.errors import Conflict, FieldError, NotFound, ValidationFailed

MAX_PAGE_SIZE = 500


@dataclass(frozen=True, slots=True)
class EventPage:
    events: list[Event]
    # Where to resume: after the last returned event, or the request's cursor when empty.
    next_cursor: EventCursor | None


def _require_filters(event_types: Sequence[str]) -> list[str]:
    if isinstance(event_types, str) or not event_types:
        raise ValidationFailed("at least one event type filter is required")
    invalid = [pattern for pattern in event_types if not is_valid_type_filter(pattern)]
    if invalid:
        raise ValidationFailed(
            "invalid event type filter",
            errors=[
                FieldError(pointer="/event_types", detail=f"{pattern!r} is not a valid filter")
                for pattern in invalid
            ],
        )
    return list(dict.fromkeys(event_types))


def _require_name(kind: str, value: str) -> str:
    try:
        return require_name(kind, value)
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from None


async def ensure_internal_subscription(
    uow: UnitOfWork,
    *,
    handler: str,
    event_types: Sequence[str],
    project_id: UUID | None = None,
    enabled: bool = True,
) -> UUID:
    """Create or update the subscription of ``handler`` (global when ``project_id`` is None)."""
    _require_name("handler", handler)
    filters = _require_filters(event_types)
    await repository.lock_fanout_exclusive(uow.session, project_id)
    return await repository.upsert_internal_subscription(
        uow.session, project_id=project_id, handler=handler, event_types=filters, enabled=enabled
    )


async def create_webhook_subscription(
    uow: UnitOfWork, *, project_id: UUID, endpoint_id: UUID, event_types: Sequence[str]
) -> UUID:
    """A webhook subscription row. Its deliveries stay ``pending`` until the sender (task 6.x)."""
    filters = _require_filters(event_types)
    await repository.lock_fanout_exclusive(uow.session, project_id)
    return await repository.insert_webhook_subscription(
        uow.session, project_id=project_id, endpoint_id=endpoint_id, event_types=filters
    )


async def set_subscription_enabled(uow: UnitOfWork, subscription_id: UUID, enabled: bool) -> None:
    scope = await repository.subscription_scope(uow.session, subscription_id)
    if scope is None:
        raise NotFound("subscription not found")
    await repository.lock_fanout_exclusive(uow.session, scope[0])
    await repository.set_subscription_enabled(uow.session, subscription_id, enabled)


async def sync_handler_subscriptions(uow: UnitOfWork, handlers: HandlerRegistry) -> list[UUID]:
    """Ensure the global subscription of every handler registered with ``event_types``."""
    return [
        await ensure_internal_subscription(uow, handler=spec.name, event_types=spec.event_types)
        for spec in handlers
        if spec.event_types
    ]


async def replay_delivery(
    uow: UnitOfWork, *, project_id: UUID, delivery_id: UUID, clock: Clock
) -> None:
    """``dead`` or ``succeeded`` back to ``pending`` with a fresh attempt budget.

    Replaying a succeeded internal delivery does not repeat its effect: the handler's inbox row
    makes the redelivery a no-op (it is for re-sending webhooks and recovering dead letters).
    """
    if await repository.replay_delivery(uow.session, project_id, delivery_id, clock.now()):
        return
    status = await repository.delivery_status(uow.session, project_id, delivery_id)
    if status is None:
        raise NotFound("delivery not found")
    raise Conflict(f"only dead or succeeded deliveries can be replayed (it is {status.value})")


async def upsert_schedule(
    uow: UnitOfWork,
    *,
    project_id: UUID,
    name: str,
    event_type: str,
    interval_seconds: int,
    next_due_at: datetime,
    enabled: bool = True,
    registry: EventRegistry = canonical_registry,
) -> UUID:
    _require_name("schedule", name)
    payload = registry.get(event_type).payload
    if not issubclass(payload, ScheduleDuePayload):
        raise ValidationFailed(f"event type {event_type!r} does not take a ScheduleDuePayload")
    if interval_seconds < 1:
        raise ValidationFailed("interval_seconds must be >= 1")
    if next_due_at.tzinfo is None:
        raise ValidationFailed("next_due_at must be timezone-aware")
    return await repository.upsert_schedule(
        uow.session,
        project_id=project_id,
        name=name,
        event_type=event_type,
        interval_seconds=interval_seconds,
        next_due_at=next_due_at,
        enabled=enabled,
    )


async def list_events_after(
    uow: UnitOfWork,
    *,
    project_id: UUID,
    cursor: EventCursor | str | None = None,
    types: Sequence[str] = (),
    subject: str | None = None,
    limit: int = 100,
) -> EventPage:
    """Page of the project's events in ``(txid, id)`` order, up to the visibility horizon."""
    position = EventCursor.decode(cursor) if isinstance(cursor, str) else cursor
    if not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValidationFailed(f"limit must be between 1 and {MAX_PAGE_SIZE}")
    filters = _require_filters(types) if types else []
    events = await repository.list_events_after(
        uow.session,
        project_id=project_id,
        cursor=position,
        types=filters,
        subject=subject,
        limit=limit,
    )
    return EventPage(events, events[-1].cursor if events else position)
