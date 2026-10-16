"""``EventPublisher.publish(uow, draft)``: the transactional outbox (ADR-0005).

The event row and one delivery per matching subscription are written in the caller's business
transaction, so they commit or roll back with the business change. Fan-out is atomic against
subscription changes: publishers hold the fan-out advisory lock *shared* (global key and the
project's key) and read subscriptions ``FOR SHARE``; subscription changes take it *exclusive*.
A subscription therefore receives every event committed after it, and none from before.

The wake-up NOTIFY is sent by an ``after_commit`` hook on a separate connection, never inside
the business transaction; if it fails, the commit stands, the failure is logged and counted,
and the dispatcher's polling picks the deliveries up.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

import structlog
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from pydantic import BaseModel

from financial_hub.modules.events.application.metrics import EventsMetrics
from financial_hub.modules.events.domain import Event
from financial_hub.modules.events.infrastructure import repository
from financial_hub.modules.events.registry import EventRegistry
from financial_hub.modules.events.registry import registry as canonical_registry
from financial_hub.platform.db import UnitOfWork
from financial_hub.shared.clock import Clock
from financial_hub.shared.errors import ValidationFailed
from financial_hub.shared.ids import new_uuid

logger = structlog.get_logger()

_propagator = TraceContextTextMapPropagator()


class Notifier(Protocol):
    async def notify(self) -> None: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class EventDraft:
    type: str
    data: BaseModel | Mapping[str, Any]
    project_id: UUID
    version: int | None = None  # None: the latest registered version
    subject: str | None = None
    occurred_at: datetime | None = None  # None: the publisher's clock
    correlation_id: UUID | None = None
    causation_id: UUID | None = None


def current_traceparent() -> str | None:
    carrier: dict[str, str] = {}
    _propagator.inject(carrier)
    return carrier.get("traceparent")


class EventPublisher:
    def __init__(
        self,
        *,
        clock: Clock,
        notifier: Notifier | None,
        registry: EventRegistry = canonical_registry,
        metrics: EventsMetrics | None = None,
    ) -> None:
        self._clock = clock
        self._notifier = notifier
        self._registry = registry
        self._metrics = metrics or EventsMetrics()

    async def publish(
        self, uow: UnitOfWork, draft: EventDraft, *, only_subscription: UUID | None = None
    ) -> Event:
        """``only_subscription``: deliver to that one subscription instead of fanning out
        (a webhook endpoint's test ping must not reach every ``*`` subscriber)."""
        event_type = self._registry.get(draft.type, draft.version)
        data = event_type.dump(draft.data)
        if draft.subject is not None and not 1 <= len(draft.subject) <= 255:
            raise ValidationFailed("event subject must have 1..255 characters")
        session = uow.session
        await repository.lock_fanout_shared(session, draft.project_id)
        now = self._clock.now()
        event_id = new_uuid()
        occurred_at = draft.occurred_at or now
        traceparent = current_traceparent()
        txid = await repository.insert_event(
            session,
            id=event_id,
            type=event_type.name,
            version=event_type.version,
            project_id=draft.project_id,
            subject=draft.subject,
            occurred_at=occurred_at,
            correlation_id=draft.correlation_id,
            causation_id=draft.causation_id,
            traceparent=traceparent,
            data=data,
        )
        subscriptions = (
            await repository.matching_subscriptions(session, draft.project_id, event_type.name)
            if only_subscription is None
            else [only_subscription]
        )
        await repository.insert_deliveries(
            session,
            event_id=event_id,
            project_id=draft.project_id,
            subscription_ids=subscriptions,
            next_attempt_at=now,
        )
        if (
            subscriptions
            and self._notifier is not None
            and self._notify_after_commit not in uow.after_commit
        ):
            uow.after_commit.append(self._notify_after_commit)
        return Event(
            id=event_id,
            type=event_type.name,
            version=event_type.version,
            project_id=draft.project_id,
            subject=draft.subject,
            occurred_at=occurred_at,
            correlation_id=draft.correlation_id,
            causation_id=draft.causation_id,
            traceparent=traceparent,
            data=data,
            txid=txid,
        )

    async def _notify_after_commit(self) -> None:
        if self._notifier is None:  # pragma: no cover - only registered with a notifier
            return
        try:
            await self._notifier.notify()
        except Exception as exc:
            self._metrics.notify_failures.add(1)
            logger.warning("events.notify_failed", error=f"{type(exc).__name__}: {exc}")
