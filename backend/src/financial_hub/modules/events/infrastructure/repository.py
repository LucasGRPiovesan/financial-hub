"""Event log persistence: plain SQL on the caller's session (the Unit of Work owns the transaction).

Runs as ``fh_app``: inserts and reads, plus UPDATEs of delivery/subscription/schedule state. Times
that drive behaviour (due, lease, backoff) come from the caller's ``Clock`` as parameters, never
from ``now()``, so tests can freeze them; audit columns (``updated_at``) use the database clock.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession

from financial_hub.modules.events.domain import (
    ClaimedDelivery,
    ClaimedWebhookDelivery,
    DeliveryStatus,
    Event,
    EventCursor,
    JsonObject,
)
from financial_hub.shared.ids import new_uuid

FANOUT_LOCK_CLASS: Final = 0x46480005
"""Advisory-lock class of fan-out: publishers hold it shared, subscription changes exclusive."""
GLOBAL_FANOUT_KEY: Final = 0

_EVENT_COLUMNS = """
    e.id, e.type, e.version, e.project_id, e.subject, e.occurred_at, e.correlation_id,
    e.causation_id, e.traceparent, e.data, e.txid::text AS txid
"""

# Same predicate as domain.matches(): '*', exact type, or 'prefix.*'.
_FILTER_MATCHES = """
    EXISTS (
        SELECT 1 FROM unnest({filters}) AS f(pattern)
        WHERE f.pattern = '*' OR f.pattern = {type}
           OR (f.pattern LIKE '%.*' AND starts_with({type}, left(f.pattern, -1)))
    )
"""

_LOCK_FANOUT_SHARED = text(
    """
    SELECT pg_catalog.pg_advisory_xact_lock_shared(:class, :global_key),
           pg_catalog.pg_advisory_xact_lock_shared(
               :class, pg_catalog.hashtext(CAST(:project AS text))
           )
    """
)
_LOCK_FANOUT_EXCLUSIVE_PROJECT = text(
    "SELECT pg_catalog.pg_advisory_xact_lock(:class, pg_catalog.hashtext(CAST(:project AS text)))"
)
_LOCK_FANOUT_EXCLUSIVE_GLOBAL = text("SELECT pg_catalog.pg_advisory_xact_lock(:class, :key)")

_INSERT_EVENT = text(
    """
    INSERT INTO public.events (id, type, version, project_id, subject, occurred_at,
                               correlation_id, causation_id, traceparent, data)
    VALUES (:id, :type, :version, :project_id, :subject, :occurred_at,
            :correlation_id, :causation_id, :traceparent, CAST(:data AS jsonb))
    RETURNING txid::text AS txid
    """
)
_MATCHING_SUBSCRIPTIONS = text(
    "".join(
        (
            """
    SELECT s.id FROM public.event_subscriptions s
    WHERE s.enabled AND (s.project_id = :project_id OR s.project_id IS NULL)
      AND """,
            _FILTER_MATCHES.format(filters="s.event_types", type=":type"),
            """
    ORDER BY s.id
    FOR SHARE OF s
    """,
        )
    )
)
_INSERT_DELIVERIES = text(
    """
    INSERT INTO public.event_deliveries (id, event_id, subscription_id, project_id, next_attempt_at)
    SELECT d.id, :event_id, d.subscription_id, :project_id, :next_attempt_at
    FROM unnest(CAST(:ids AS uuid[]), CAST(:subscription_ids AS uuid[]))
        AS d(id, subscription_id)
    """
)

_CLAIM = text(
    "".join(
        (
            """
    WITH due AS (
        SELECT d.id FROM public.event_deliveries d
        JOIN public.event_subscriptions s ON s.id = d.subscription_id
        WHERE s.kind = 'internal' AND s.handler = ANY (CAST(:handlers AS text[]))
          AND ((d.status IN ('pending', 'retry_scheduled') AND d.next_attempt_at <= :now)
               OR (d.status = 'leased' AND d.lease_until < :now))
        ORDER BY d.next_attempt_at, d.id
        LIMIT :limit
        FOR UPDATE OF d SKIP LOCKED
    )
    UPDATE public.event_deliveries d
    SET status = 'leased', lease_token = pg_catalog.gen_random_uuid(),
        lease_until = :lease_until, attempts = d.attempts + 1, updated_at = now()
    FROM due, public.events e, public.event_subscriptions s
    WHERE d.id = due.id AND e.id = d.event_id AND s.id = d.subscription_id
    RETURNING d.id AS delivery_id, d.subscription_id, s.handler, d.lease_token, d.lease_until,
              d.attempts, """,
            _EVENT_COLUMNS,
        )
    )
)
_COMPLETE_SUCCEEDED = text(
    """
    UPDATE public.event_deliveries
    SET status = 'succeeded', lease_token = NULL, lease_until = NULL, last_error = NULL,
        updated_at = now()
    WHERE id = :id AND lease_token = :token AND status = 'leased'
    """
)
_COMPLETE_FAILED = text(
    """
    UPDATE public.event_deliveries
    SET status = :status, next_attempt_at = :next_attempt_at, lease_token = NULL,
        lease_until = NULL, last_error = :error, updated_at = now()
    WHERE id = :id AND lease_token = :token AND status = 'leased'
    """
)
_RELEASE = text(
    """
    UPDATE public.event_deliveries
    SET status = 'pending', attempts = greatest(attempts - 1, 0), lease_token = NULL,
        lease_until = NULL, updated_at = now()
    WHERE id = :id AND lease_token = :token AND status = 'leased'
    """
)
_INSERT_INBOX = text(
    """
    INSERT INTO public.inbox (handler, event_id) VALUES (:handler, :event_id)
    ON CONFLICT (handler, event_id) DO NOTHING
    RETURNING 1
    """
)
_REPLAY = text(
    """
    UPDATE public.event_deliveries
    SET status = 'pending', attempts = 0, next_attempt_at = :now, last_error = NULL,
        updated_at = now()
    WHERE id = :id AND project_id = :project_id AND status IN ('dead', 'succeeded')
    RETURNING id
    """
)
_DELIVERY_STATUS = text(
    "SELECT status FROM public.event_deliveries WHERE id = :id AND project_id = :project_id"
)
_STATS = text(
    """
    SELECT
        (SELECT min(next_attempt_at) FROM public.event_deliveries
         WHERE status IN ('pending', 'retry_scheduled') AND next_attempt_at <= :now) AS oldest_due,
        (SELECT count(*) FROM public.event_deliveries WHERE status = 'pending') AS pending,
        (SELECT count(*) FROM public.event_deliveries WHERE status = 'leased') AS leased,
        (SELECT count(*) FROM public.event_deliveries
         WHERE status = 'retry_scheduled') AS retry_scheduled,
        (SELECT count(*) FROM public.event_deliveries WHERE status = 'dead') AS dead,
        pg_catalog.pg_notification_queue_usage() AS queue_usage
    """
)

_UPSERT_INTERNAL_SUBSCRIPTION = text(
    """
    INSERT INTO public.event_subscriptions (id, project_id, kind, handler, event_types, enabled)
    VALUES (:id, :project_id, 'internal', :handler, CAST(:event_types AS text[]), :enabled)
    ON CONFLICT ON CONSTRAINT uq_event_subscriptions_target
    DO UPDATE SET event_types = EXCLUDED.event_types, enabled = EXCLUDED.enabled,
                  updated_at = now()
    RETURNING id
    """
)
_INSERT_WEBHOOK_SUBSCRIPTION = text(
    """
    INSERT INTO public.event_subscriptions (id, project_id, kind, endpoint_id, event_types)
    VALUES (:id, :project_id, 'webhook', :endpoint_id, CAST(:event_types AS text[]))
    RETURNING id
    """
)
_SUBSCRIPTION_SCOPE = text("SELECT project_id FROM public.event_subscriptions WHERE id = :id")
_SET_SUBSCRIPTION_ENABLED = text(
    """
    UPDATE public.event_subscriptions SET enabled = :enabled, updated_at = now()
    WHERE id = :id
    """
)

_UPSERT_SCHEDULE = text(
    """
    INSERT INTO public.schedules (id, project_id, name, event_type, interval_seconds, next_due_at,
                                  enabled)
    VALUES (:id, :project_id, :name, :event_type, :interval_seconds, :next_due_at, :enabled)
    ON CONFLICT ON CONSTRAINT uq_schedules_project_id_name
    DO UPDATE SET event_type = EXCLUDED.event_type, interval_seconds = EXCLUDED.interval_seconds,
                  next_due_at = EXCLUDED.next_due_at, enabled = EXCLUDED.enabled,
                  updated_at = now()
    RETURNING id
    """
)
_DUE_SCHEDULE_IDS = text(
    """
    SELECT id FROM public.schedules
    WHERE enabled AND next_due_at <= :now
    ORDER BY next_due_at, id
    LIMIT :limit
    """
)
_LOCK_DUE_SCHEDULE = text(
    """
    SELECT id, project_id, name, event_type, interval_seconds, next_due_at
    FROM public.schedules
    WHERE id = :id AND enabled AND next_due_at <= :now
    FOR UPDATE SKIP LOCKED
    """
)
_RUN_EXISTS = text(
    "SELECT 1 FROM public.schedule_runs WHERE schedule_id = :schedule_id AND due_at = :due_at"
)
_INSERT_RUN = text(
    """
    INSERT INTO public.schedule_runs (schedule_id, due_at, event_id)
    VALUES (:schedule_id, :due_at, :event_id)
    """
)
_ADVANCE_SCHEDULE = text(
    "UPDATE public.schedules SET next_due_at = :next_due_at, updated_at = now() WHERE id = :id"
)


@dataclass(frozen=True, slots=True)
class DeliveryStats:
    oldest_due: datetime | None
    by_status: dict[DeliveryStatus, int]
    notification_queue_usage: float


@dataclass(frozen=True, slots=True)
class DueSchedule:
    id: UUID
    project_id: UUID
    name: str
    event_type: str
    interval_seconds: int
    next_due_at: datetime


def _event(row: RowMapping) -> Event:
    return Event(
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
    )


# --- publishing ------------------------------------------------------------------------------


async def lock_fanout_shared(session: AsyncSession, project_id: UUID) -> None:
    await session.execute(
        _LOCK_FANOUT_SHARED,
        {"class": FANOUT_LOCK_CLASS, "global_key": GLOBAL_FANOUT_KEY, "project": project_id},
    )


async def lock_fanout_exclusive(session: AsyncSession, project_id: UUID | None) -> None:
    if project_id is None:
        await session.execute(
            _LOCK_FANOUT_EXCLUSIVE_GLOBAL, {"class": FANOUT_LOCK_CLASS, "key": GLOBAL_FANOUT_KEY}
        )
    else:
        await session.execute(
            _LOCK_FANOUT_EXCLUSIVE_PROJECT, {"class": FANOUT_LOCK_CLASS, "project": project_id}
        )


async def insert_event(
    session: AsyncSession,
    *,
    id: UUID,
    type: str,
    version: int,
    project_id: UUID,
    subject: str | None,
    occurred_at: datetime,
    correlation_id: UUID | None,
    causation_id: UUID | None,
    traceparent: str | None,
    data: JsonObject,
) -> int:
    """Insert one event; returns its ``txid`` (the inserting transaction's id)."""
    result = await session.execute(
        _INSERT_EVENT,
        {
            "id": id,
            "type": type,
            "version": version,
            "project_id": project_id,
            "subject": subject,
            "occurred_at": occurred_at,
            "correlation_id": correlation_id,
            "causation_id": causation_id,
            "traceparent": traceparent,
            "data": json.dumps(data, separators=(",", ":")),
        },
    )
    return int(result.scalar_one())


async def matching_subscriptions(
    session: AsyncSession, project_id: UUID, event_type: str
) -> list[UUID]:
    """Enabled subscriptions for the event, locked ``FOR SHARE`` until the business commit."""
    result = await session.execute(
        _MATCHING_SUBSCRIPTIONS, {"project_id": project_id, "type": event_type}
    )
    return list(result.scalars())


async def insert_deliveries(
    session: AsyncSession,
    *,
    event_id: UUID,
    project_id: UUID,
    subscription_ids: Sequence[UUID],
    next_attempt_at: datetime,
) -> None:
    if not subscription_ids:
        return
    await session.execute(
        _INSERT_DELIVERIES,
        {
            "event_id": event_id,
            "project_id": project_id,
            "next_attempt_at": next_attempt_at,
            "ids": [new_uuid() for _ in subscription_ids],
            "subscription_ids": list(subscription_ids),
        },
    )


# --- dispatching -----------------------------------------------------------------------------


async def claim_deliveries(
    session: AsyncSession,
    *,
    handlers: Sequence[str],
    now: datetime,
    lease_until: datetime,
    limit: int,
) -> list[ClaimedDelivery]:
    """Lease due (or lease-expired) deliveries of the given handlers with fresh tokens."""
    if not handlers or limit < 1:
        return []
    result = await session.execute(
        _CLAIM,
        {
            "handlers": list(handlers),
            "now": now,
            "lease_until": lease_until,
            "limit": limit,
        },
    )
    claimed = [
        ClaimedDelivery(
            id=row["delivery_id"],
            event=_event(row),
            subscription_id=row["subscription_id"],
            handler=row["handler"],
            lease_token=row["lease_token"],
            lease_until=row["lease_until"],
            attempts=row["attempts"],
        )
        for row in result.mappings()
    ]
    claimed.sort(key=lambda c: (c.event.txid, c.event.id, c.id))
    return claimed


async def complete_succeeded(session: AsyncSession, delivery_id: UUID, token: UUID) -> bool:
    """Compare-and-set on the lease token; ``False`` means this worker was fenced out."""
    result = await session.execute(_COMPLETE_SUCCEEDED, {"id": delivery_id, "token": token})
    return _rowcount(result) == 1


async def complete_failed(
    session: AsyncSession,
    delivery_id: UUID,
    token: UUID,
    *,
    status: DeliveryStatus,
    next_attempt_at: datetime,
    error: str,
) -> bool:
    result = await session.execute(
        _COMPLETE_FAILED,
        {
            "id": delivery_id,
            "token": token,
            "status": status.value,
            "next_attempt_at": next_attempt_at,
            "error": error,
        },
    )
    return _rowcount(result) == 1


async def release_lease(session: AsyncSession, delivery_id: UUID, token: UUID) -> bool:
    result = await session.execute(_RELEASE, {"id": delivery_id, "token": token})
    return _rowcount(result) == 1


async def insert_inbox(session: AsyncSession, handler: str, event_id: UUID) -> bool:
    """``True`` when this is the first time ``handler`` sees ``event_id`` (ADR-0004)."""
    result = await session.execute(_INSERT_INBOX, {"handler": handler, "event_id": event_id})
    return result.scalar_one_or_none() is not None


async def replay_delivery(
    session: AsyncSession, project_id: UUID, delivery_id: UUID, now: datetime
) -> bool:
    result = await session.execute(
        _REPLAY, {"id": delivery_id, "project_id": project_id, "now": now}
    )
    return result.scalar_one_or_none() is not None


async def delivery_status(
    session: AsyncSession, project_id: UUID, delivery_id: UUID
) -> DeliveryStatus | None:
    result = await session.execute(_DELIVERY_STATUS, {"id": delivery_id, "project_id": project_id})
    value = result.scalar_one_or_none()
    return None if value is None else DeliveryStatus(value)


async def delivery_stats(session: AsyncSession, now: datetime) -> DeliveryStats:
    row = (await session.execute(_STATS, {"now": now})).mappings().one()
    return DeliveryStats(
        oldest_due=row["oldest_due"],
        by_status={
            status: int(row[status.value])
            for status in (
                DeliveryStatus.PENDING,
                DeliveryStatus.LEASED,
                DeliveryStatus.RETRY_SCHEDULED,
                DeliveryStatus.DEAD,
            )
        },
        notification_queue_usage=float(row["queue_usage"]),
    )


# --- reading ---------------------------------------------------------------------------------


async def list_events_after(
    session: AsyncSession,
    *,
    project_id: UUID,
    cursor: EventCursor | None,
    types: Sequence[str],
    subject: str | None,
    limit: int,
) -> list[Event]:
    """Events after ``cursor`` in ``(txid, id)`` order, below the visibility horizon only.

    ``txid < pg_snapshot_xmin(pg_current_snapshot())``: every transaction that could still
    commit an event with a smaller ``txid`` has finished, so a cursor never moves past an event
    that becomes visible later (ADR-0005).
    """
    clauses = [
        "e.project_id = :project_id",
        "e.txid < pg_catalog.pg_snapshot_xmin(pg_catalog.pg_current_snapshot())",
    ]
    params: dict[str, Any] = {"project_id": project_id, "limit": limit}
    if cursor is not None:
        clauses.append("(e.txid, e.id) > (CAST(:cursor_txid AS xid8), :cursor_id)")
        params |= {"cursor_txid": str(cursor.txid), "cursor_id": cursor.id}
    if types:
        clauses.append(_FILTER_MATCHES.format(filters="CAST(:types AS text[])", type="e.type"))
        params["types"] = list(types)
    if subject is not None:
        clauses.append("e.subject = :subject")
        params["subject"] = subject
    statement = text(
        f"SELECT {_EVENT_COLUMNS} FROM public.events e WHERE "  # noqa: S608 - fixed fragments
        + " AND ".join(clauses)
        + " ORDER BY e.txid, e.id LIMIT :limit"
    )
    result = await session.execute(statement, params)
    return [_event(row) for row in result.mappings()]


async def get_event(session: AsyncSession, project_id: UUID, event_id: UUID) -> Event | None:
    result = await session.execute(
        text(
            f"SELECT {_EVENT_COLUMNS} FROM public.events e "  # noqa: S608 - fixed fragment
            "WHERE e.id = :id AND e.project_id = :project_id"
        ),
        {"id": event_id, "project_id": project_id},
    )
    row = result.mappings().one_or_none()
    return None if row is None else _event(row)


# --- subscriptions ---------------------------------------------------------------------------


async def upsert_internal_subscription(
    session: AsyncSession,
    *,
    project_id: UUID | None,
    handler: str,
    event_types: Sequence[str],
    enabled: bool,
) -> UUID:
    result = await session.execute(
        _UPSERT_INTERNAL_SUBSCRIPTION,
        {
            "id": new_uuid(),
            "project_id": project_id,
            "handler": handler,
            "event_types": list(event_types),
            "enabled": enabled,
        },
    )
    return result.scalar_one()


async def insert_webhook_subscription(
    session: AsyncSession, *, project_id: UUID, endpoint_id: UUID, event_types: Sequence[str]
) -> UUID:
    result = await session.execute(
        _INSERT_WEBHOOK_SUBSCRIPTION,
        {
            "id": new_uuid(),
            "project_id": project_id,
            "endpoint_id": endpoint_id,
            "event_types": list(event_types),
        },
    )
    return result.scalar_one()


async def subscription_scope(
    session: AsyncSession, subscription_id: UUID
) -> tuple[UUID | None] | None:
    """``(project_id,)`` of the subscription (immutable, so read unlocked); ``None`` if absent.

    Callers take the fan-out advisory lock *before* touching the row: publishers lock advisory
    first and rows second, so the reverse order here could deadlock.
    """
    row = (await session.execute(_SUBSCRIPTION_SCOPE, {"id": subscription_id})).one_or_none()
    return None if row is None else (row[0],)


async def set_subscription_enabled(
    session: AsyncSession, subscription_id: UUID, enabled: bool
) -> None:
    await session.execute(_SET_SUBSCRIPTION_ENABLED, {"id": subscription_id, "enabled": enabled})


# --- schedules -------------------------------------------------------------------------------


async def upsert_schedule(
    session: AsyncSession,
    *,
    project_id: UUID,
    name: str,
    event_type: str,
    interval_seconds: int,
    next_due_at: datetime,
    enabled: bool,
) -> UUID:
    result = await session.execute(
        _UPSERT_SCHEDULE,
        {
            "id": new_uuid(),
            "project_id": project_id,
            "name": name,
            "event_type": event_type,
            "interval_seconds": interval_seconds,
            "next_due_at": next_due_at,
            "enabled": enabled,
        },
    )
    return result.scalar_one()


async def due_schedule_ids(session: AsyncSession, now: datetime, limit: int) -> list[UUID]:
    result = await session.execute(_DUE_SCHEDULE_IDS, {"now": now, "limit": limit})
    return list(result.scalars())


async def lock_due_schedule(
    session: AsyncSession, schedule_id: UUID, now: datetime
) -> DueSchedule | None:
    """The schedule if it is still due and no other scheduler holds it (``SKIP LOCKED``)."""
    result = await session.execute(_LOCK_DUE_SCHEDULE, {"id": schedule_id, "now": now})
    row = result.mappings().one_or_none()
    if row is None:
        return None
    return DueSchedule(
        id=row["id"],
        project_id=row["project_id"],
        name=row["name"],
        event_type=row["event_type"],
        interval_seconds=row["interval_seconds"],
        next_due_at=row["next_due_at"],
    )


async def schedule_run_exists(session: AsyncSession, schedule_id: UUID, due_at: datetime) -> bool:
    result = await session.execute(_RUN_EXISTS, {"schedule_id": schedule_id, "due_at": due_at})
    return result.scalar_one_or_none() is not None


async def insert_schedule_run(
    session: AsyncSession, schedule_id: UUID, due_at: datetime, event_id: UUID
) -> None:
    await session.execute(
        _INSERT_RUN, {"schedule_id": schedule_id, "due_at": due_at, "event_id": event_id}
    )


async def advance_schedule(session: AsyncSession, schedule_id: UUID, next_due_at: datetime) -> None:
    await session.execute(_ADVANCE_SCHEDULE, {"id": schedule_id, "next_due_at": next_due_at})


def _rowcount(result: Any) -> int:
    return int(result.rowcount)


# --- webhook deliveries (claimed by the webhooks module's sender, W6.3) -----------------------

_CLAIM_WEBHOOK = text(
    "".join(
        (
            """
    WITH due AS (
        SELECT d.id FROM public.event_deliveries d
        JOIN public.event_subscriptions s ON s.id = d.subscription_id
        WHERE s.kind = 'webhook' AND s.enabled
          AND ((d.status IN ('pending', 'retry_scheduled') AND d.next_attempt_at <= :now)
               OR (d.status = 'leased' AND d.lease_until < :now))
        ORDER BY d.next_attempt_at, d.id
        LIMIT :limit
        FOR UPDATE OF d SKIP LOCKED
    )
    UPDATE public.event_deliveries d
    SET status = 'leased', lease_token = pg_catalog.gen_random_uuid(),
        lease_until = :lease_until, attempts = d.attempts + 1, updated_at = now()
    FROM due, public.events e, public.event_subscriptions s
    WHERE d.id = due.id AND e.id = d.event_id AND s.id = d.subscription_id
    RETURNING d.id AS delivery_id, d.subscription_id, s.endpoint_id, d.lease_token,
              d.lease_until, d.attempts, e.recorded_at, """,
            _EVENT_COLUMNS,
        )
    )
)
_WEBHOOK_DELIVERY_ENDPOINT = text(
    """
    SELECT s.endpoint_id FROM public.event_deliveries d
    JOIN public.event_subscriptions s ON s.id = d.subscription_id
    WHERE d.id = :id AND d.event_id = :event_id AND d.project_id = :project_id
      AND s.kind = 'webhook'
    """
)
_SET_SUBSCRIPTION_EVENT_TYPES = text(
    """
    UPDATE public.event_subscriptions
    SET event_types = CAST(:event_types AS text[]), updated_at = now()
    WHERE id = :id
    """
)


async def claim_webhook_deliveries(
    session: AsyncSession, *, now: datetime, lease_until: datetime, limit: int
) -> list[ClaimedWebhookDelivery]:
    """Lease due (or lease-expired) deliveries of *enabled* webhook subscriptions."""
    if limit < 1:
        return []
    result = await session.execute(
        _CLAIM_WEBHOOK, {"now": now, "lease_until": lease_until, "limit": limit}
    )
    claimed = [
        ClaimedWebhookDelivery(
            id=row["delivery_id"],
            event=_event(row),
            recorded_at=row["recorded_at"],
            subscription_id=row["subscription_id"],
            endpoint_id=row["endpoint_id"],
            lease_token=row["lease_token"],
            lease_until=row["lease_until"],
            attempts=row["attempts"],
        )
        for row in result.mappings()
    ]
    claimed.sort(key=lambda c: (c.event.txid, c.event.id, c.id))
    return claimed


async def webhook_delivery_endpoint(
    session: AsyncSession, *, project_id: UUID, event_id: UUID, delivery_id: UUID
) -> UUID | None:
    """Endpoint id of a webhook delivery of ``event_id`` in the project; ``None`` otherwise."""
    result = await session.execute(
        _WEBHOOK_DELIVERY_ENDPOINT,
        {"id": delivery_id, "event_id": event_id, "project_id": project_id},
    )
    return result.scalar_one_or_none()


async def set_subscription_event_types(
    session: AsyncSession, subscription_id: UUID, event_types: Sequence[str]
) -> None:
    await session.execute(
        _SET_SUBSCRIPTION_EVENT_TYPES, {"id": subscription_id, "event_types": list(event_types)}
    )
