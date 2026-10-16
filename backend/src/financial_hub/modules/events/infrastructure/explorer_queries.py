"""Event Explorer read queries: project-scoped SELECTs on the caller's session (as ``fh_app``).

Every query here is read-only: no locks, no writes, no schema changes (W3.3 / PRO-39). The list
query pages by ``(txid, id)`` *descending* and only below the visibility horizon (ADR-0005), the
same predicate the SSE reader and ``repository.list_events_after`` use.

The ledger linkage uses ``journal_entries.causation_id`` / ``correlation_id`` (same UUID domain as
``events``, ADR-0003/ADR-0005): a journal entry is linked to a set of events when its
``causation_id`` is one of the event ids or its ``correlation_id`` is one of the events'
correlation ids. Raw SQL keeps this decoupled from ``modules.ledger`` (no cross-module imports).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession

from financial_hub.modules.events.domain import Event, EventCursor

_EVENT_COLUMNS: Final = """
    e.id, e.type, e.version, e.project_id, e.subject, e.occurred_at, e.correlation_id,
    e.causation_id, e.traceparent, e.data, e.txid::text AS txid, e.recorded_at
"""
_HORIZON: Final = "e.txid < pg_catalog.pg_snapshot_xmin(pg_catalog.pg_current_snapshot())"


@dataclass(frozen=True, slots=True)
class EventRecord:
    """An event row plus its database ``recorded_at`` (not part of the domain ``Event``)."""

    event: Event
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class DeliveryRecord:
    """One delivery row joined with its subscription's identity."""

    id: UUID
    subscription_id: UUID
    kind: str
    handler: str | None
    endpoint_id: UUID | None
    status: str
    attempts: int
    next_attempt_at: datetime
    last_error: str | None


@dataclass(frozen=True, slots=True)
class JournalEntryRecord:
    """The journal-entry fields the explorer renders (no postings)."""

    id: UUID
    project_id: UUID
    status: str
    recipe: str
    effective_at: datetime
    reverses_entry_id: UUID | None
    correlation_id: UUID | None
    causation_id: UUID | None


def _event(row: RowMapping) -> EventRecord:
    return EventRecord(
        Event(
            id=row["id"],
            type=row["type"],
            version=row["version"],
            project_id=row["project_id"],
            subject=row["subject"],
            occurred_at=row["occurred_at"],
            correlation_id=row["correlation_id"],
            causation_id=row["causation_id"],
            traceparent=row["traceparent"],
            data=row["data"],
            txid=int(row["txid"]),
        ),
        row["recorded_at"],
    )


_EVENT_BY_ID = text(
    f"SELECT {_EVENT_COLUMNS} FROM public.events e "  # noqa: S608 - fixed fragments
    "WHERE e.id = :id AND e.project_id = :project_id"
)
_CHILDREN = text(
    f"SELECT {_EVENT_COLUMNS} FROM public.events e "  # noqa: S608 - fixed fragments
    "WHERE e.causation_id = :parent_id AND e.project_id = :project_id "
    "ORDER BY e.txid, e.id"
)
_CORRELATION_CHAIN = text(
    f"SELECT {_EVENT_COLUMNS} FROM public.events e "  # noqa: S608 - fixed fragments
    "WHERE e.correlation_id = :correlation_id AND e.project_id = :project_id "
    "ORDER BY e.txid, e.id"
)


async def get_event(
    session: AsyncSession, *, project_id: UUID, event_id: UUID
) -> EventRecord | None:
    row = (
        (await session.execute(_EVENT_BY_ID, {"id": event_id, "project_id": project_id}))
        .mappings()
        .one_or_none()
    )
    return None if row is None else _event(row)


async def children_of(
    session: AsyncSession, *, project_id: UUID, parent_id: UUID
) -> list[EventRecord]:
    """Events of the project whose ``causation_id`` is ``parent_id``, in commit order."""
    result = await session.execute(_CHILDREN, {"parent_id": parent_id, "project_id": project_id})
    return [_event(row) for row in result.mappings()]


async def correlation_chain(
    session: AsyncSession, *, project_id: UUID, correlation_id: UUID
) -> list[EventRecord]:
    """Every event of the project carrying ``correlation_id``, in commit order."""
    result = await session.execute(
        _CORRELATION_CHAIN, {"correlation_id": correlation_id, "project_id": project_id}
    )
    return [_event(row) for row in result.mappings()]


async def list_events(
    session: AsyncSession,
    *,
    project_id: UUID,
    cursor: EventCursor | None,
    event_type: str | None,
    correlation_id: UUID | None,
    subject: str | None,
    occurred_from: datetime | None,
    occurred_to: datetime | None,
    limit: int,
) -> list[EventRecord]:
    """Newest-first page of the project's events below the visibility horizon.

    ``event_type`` is one of ``*``, an exact type, or ``prefix.*`` (already validated); ``*``
    drops the filter. ``occurred_from``/``occurred_to`` are inclusive. Callers fetch ``limit + 1``
    rows and drop the extra one to learn whether another page exists.
    """
    clauses = ["e.project_id = :project_id", _HORIZON]
    params: dict[str, Any] = {"project_id": project_id, "limit": limit}
    if cursor is not None:
        clauses.append("(e.txid, e.id) < (CAST(:cursor_txid AS xid8), :cursor_id)")
        params["cursor_txid"] = str(cursor.txid)
        params["cursor_id"] = cursor.id
    if event_type is not None and event_type != "*":
        if event_type.endswith(".*"):
            clauses.append("starts_with(e.type, :type_prefix)")
            params["type_prefix"] = event_type[:-1]
        else:
            clauses.append("e.type = :type")
            params["type"] = event_type
    if correlation_id is not None:
        clauses.append("e.correlation_id = :correlation_id")
        params["correlation_id"] = correlation_id
    if subject is not None:
        clauses.append("e.subject = :subject")
        params["subject"] = subject
    if occurred_from is not None:
        clauses.append("e.occurred_at >= :occurred_from")
        params["occurred_from"] = occurred_from
    if occurred_to is not None:
        clauses.append("e.occurred_at <= :occurred_to")
        params["occurred_to"] = occurred_to
    statement = text(
        f"SELECT {_EVENT_COLUMNS} FROM public.events e WHERE "  # noqa: S608 - fixed fragments
        + " AND ".join(clauses)
        + " ORDER BY e.txid DESC, e.id DESC LIMIT :limit"
    )
    result = await session.execute(statement, params)
    return [_event(row) for row in result.mappings()]


_DELIVERIES = text(
    """
    SELECT d.id AS delivery_id, d.subscription_id, s.kind, s.handler, s.endpoint_id,
           d.status, d.attempts, d.next_attempt_at, d.last_error
    FROM public.event_deliveries d
    JOIN public.event_subscriptions s ON s.id = d.subscription_id
    WHERE d.event_id = :event_id AND d.project_id = :project_id
    ORDER BY d.created_at, d.id
    """
)


async def list_deliveries(
    session: AsyncSession, *, project_id: UUID, event_id: UUID
) -> list[DeliveryRecord]:
    result = await session.execute(_DELIVERIES, {"event_id": event_id, "project_id": project_id})
    return [
        DeliveryRecord(
            id=row["delivery_id"],
            subscription_id=row["subscription_id"],
            kind=row["kind"],
            handler=row["handler"],
            endpoint_id=row["endpoint_id"],
            status=row["status"],
            attempts=row["attempts"],
            next_attempt_at=row["next_attempt_at"],
            last_error=row["last_error"],
        )
        for row in result.mappings()
    ]


_ENTRY_COLUMNS: Final = """
    j.id, j.project_id, j.status, j.recipe, j.effective_at, j.reverses_entry_id,
    j.correlation_id, j.causation_id
"""


async def linked_journal_entries(
    session: AsyncSession,
    *,
    project_id: UUID,
    causation_ids: Sequence[UUID],
    correlation_ids: Sequence[UUID],
) -> list[JournalEntryRecord]:
    """Journal entries linked to the given events: caused by an event id or sharing a correlation.

    ``None`` values are ignored; with nothing to match an empty list is returned (the ``uuid[]``
    cast would not accept an empty Python list).
    """
    wanted_causes = sorted(set(causation_ids))
    wanted_corrs = sorted(set(correlation_ids))
    if not wanted_causes and not wanted_corrs:
        return []
    linkage: list[str] = []
    params: dict[str, Any] = {"project_id": project_id}
    if wanted_causes:
        linkage.append("j.causation_id = ANY(CAST(:causation_ids AS uuid[]))")
        params["causation_ids"] = wanted_causes
    if wanted_corrs:
        linkage.append("j.correlation_id = ANY(CAST(:correlation_ids AS uuid[]))")
        params["correlation_ids"] = wanted_corrs
    statement = text(
        f"SELECT {_ENTRY_COLUMNS} FROM public.journal_entries j "  # noqa: S608 - fixed fragments
        + "WHERE j.project_id = :project_id AND ("
        + " OR ".join(linkage)
        + ") ORDER BY j.created_at, j.id"
    )
    result = await session.execute(statement, params)
    return [
        JournalEntryRecord(
            id=row["id"],
            project_id=row["project_id"],
            status=row["status"],
            recipe=row["recipe"],
            effective_at=row["effective_at"],
            reverses_entry_id=row["reverses_entry_id"],
            correlation_id=row["correlation_id"],
            causation_id=row["causation_id"],
        )
        for row in result.mappings()
    ]
