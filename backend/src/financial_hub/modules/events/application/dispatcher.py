"""Delivery dispatcher (ADR-0005, review finding F-22): lease, run, complete with fencing.

1. **Claim** (own short transaction): due ``pending``/``retry_scheduled`` deliveries and leases
   that expired (a crashed worker) are selected ``FOR UPDATE SKIP LOCKED``, given a fresh
   ``lease_token`` and ``lease_until``, ``attempts + 1``, and committed.
2. **Process** (one transaction per delivery): first a compare-and-set completes the delivery
   only if it still carries *our* token. That statement also row-locks it, so no other worker
   can re-claim it while the handler runs. Then the ``inbox`` row is written (a duplicate skips
   the handler, success) and the handler runs on the same Unit of Work. Commit makes effect,
   inbox and completion durable together. A lost CAS means another worker re-leased the
   delivery: the handler is not run, ``dispatcher_fenced_total`` is incremented, and nothing
   of ours is kept.
3. **Failure**: rollback, then a CAS moves the delivery to ``retry_scheduled`` with exponential
   backoff and full jitter, or to ``dead`` once ``max_attempts`` is reached.

Only deliveries of internal subscriptions whose handler this process knows are claimed, so an
unknown handler or a webhook subscription (task 6.x) never burns attempts here.
"""

import asyncio
import contextlib
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from financial_hub.modules.events.application.handlers import HandlerRegistry
from financial_hub.modules.events.application.metrics import EventsMetrics
from financial_hub.modules.events.domain import (
    ClaimedDelivery,
    DeliveryStatus,
    RetryPolicy,
    truncate_error,
)
from financial_hub.modules.events.infrastructure import repository
from financial_hub.modules.events.infrastructure.pg import PgListener
from financial_hub.platform.db import UnitOfWork
from financial_hub.shared.clock import Clock

logger = structlog.get_logger()


class Outcome(StrEnum):
    SUCCEEDED = "succeeded"
    DUPLICATE = "duplicate"
    RETRY_SCHEDULED = "retry_scheduled"
    DEAD = "dead"
    FENCED = "fenced"
    RELEASED = "released"


@dataclass(frozen=True, slots=True)
class DispatcherConfig:
    batch_size: int = 50
    lease: timedelta = timedelta(seconds=60)
    handler_timeout_seconds: float = 30.0
    poll_interval_seconds: float = 2.0
    stats_interval_seconds: float = 15.0
    retry: RetryPolicy = field(default_factory=RetryPolicy)


class _Fenced(Exception):
    """The lease token changed: roll back and discard this worker's result."""


class Dispatcher:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        handlers: HandlerRegistry,
        *,
        clock: Clock,
        config: DispatcherConfig | None = None,
        metrics: EventsMetrics | None = None,
        listener: PgListener | None = None,
        uniform: Callable[[], float] = random.random,
    ) -> None:
        self._session_factory = session_factory
        self._handlers = handlers
        self._clock = clock
        self._config = config or DispatcherConfig()
        self._metrics = metrics or EventsMetrics()
        self._listener = listener
        self._uniform = uniform

    @property
    def config(self) -> DispatcherConfig:
        return self._config

    async def claim(self, limit: int | None = None) -> list[ClaimedDelivery]:
        now = self._clock.now()
        async with UnitOfWork(self._session_factory) as uow:
            return await repository.claim_deliveries(
                uow.session,
                handlers=self._handlers.names,
                now=now,
                lease_until=now + self._config.lease,
                limit=self._config.batch_size if limit is None else limit,
            )

    async def process(self, delivery: ClaimedDelivery) -> Outcome:
        handler = self._handlers.get(delivery.handler)
        self._metrics.attempts.record(delivery.attempts, {"handler": delivery.handler})
        log = logger.bind(
            delivery_id=str(delivery.id),
            event_id=delivery.event.public_id,
            event_type=delivery.event.type,
            handler=delivery.handler,
            attempt=delivery.attempts,
        )
        if handler is None:  # claimed by name, so only if the registry changed meanwhile
            return await self._fail(delivery, f"handler {delivery.handler!r} is not registered")
        if delivery.attempts > self._config.retry.max_attempts:
            # Leases kept expiring (the worker died mid-handler): stop before another crash.
            return await self._fail(delivery, "lease expired after the last attempt")
        duplicate = False
        try:
            async with UnitOfWork(self._session_factory) as uow:
                session = uow.session
                if not await repository.complete_succeeded(
                    session, delivery.id, delivery.lease_token
                ):
                    raise _Fenced
                if await repository.insert_inbox(session, delivery.handler, delivery.event.id):
                    async with asyncio.timeout(self._config.handler_timeout_seconds):
                        await handler(delivery.event, uow)
                else:
                    duplicate = True
        except _Fenced:
            self._fenced(delivery)
            return Outcome.FENCED
        except Exception as exc:
            log.warning("dispatcher.delivery_failed", exc_info=exc)
            return await self._fail(delivery, f"{type(exc).__name__}: {exc}")
        outcome = Outcome.DUPLICATE if duplicate else Outcome.SUCCEEDED
        self._completed(delivery, outcome)
        log.debug("dispatcher.delivery_completed", outcome=outcome.value)
        return outcome

    async def _fail(self, delivery: ClaimedDelivery, error: str) -> Outcome:
        now = self._clock.now()
        policy = self._config.retry
        if policy.exhausted(delivery.attempts):
            status, next_attempt_at = DeliveryStatus.DEAD, now
        else:
            status = DeliveryStatus.RETRY_SCHEDULED
            next_attempt_at = now + policy.delay(delivery.attempts, self._uniform)
        async with UnitOfWork(self._session_factory) as uow:
            completed = await repository.complete_failed(
                uow.session,
                delivery.id,
                delivery.lease_token,
                status=status,
                next_attempt_at=next_attempt_at,
                error=truncate_error(error),
            )
        if not completed:
            self._fenced(delivery)
            return Outcome.FENCED
        outcome = Outcome.DEAD if status is DeliveryStatus.DEAD else Outcome.RETRY_SCHEDULED
        self._completed(delivery, outcome)
        if outcome is Outcome.DEAD:
            logger.error(
                "dispatcher.delivery_dead",
                delivery_id=str(delivery.id),
                event_id=delivery.event.public_id,
                handler=delivery.handler,
                attempts=delivery.attempts,
                error=truncate_error(error, 500),
            )
        return outcome

    async def release(self, delivery: ClaimedDelivery) -> Outcome:
        """Give an unprocessed lease back (shutdown) without spending its attempt."""
        async with UnitOfWork(self._session_factory) as uow:
            released = await repository.release_lease(
                uow.session, delivery.id, delivery.lease_token
            )
        return Outcome.RELEASED if released else Outcome.FENCED

    def _fenced(self, delivery: ClaimedDelivery) -> None:
        self._metrics.fenced.add(1, {"handler": delivery.handler})
        logger.warning(
            "dispatcher.fenced",
            delivery_id=str(delivery.id),
            event_id=delivery.event.public_id,
            handler=delivery.handler,
        )

    def _completed(self, delivery: ClaimedDelivery, outcome: Outcome) -> None:
        self._metrics.completed.add(1, {"handler": delivery.handler, "outcome": outcome.value})

    async def run_once(self, stop: asyncio.Event | None = None) -> list[Outcome]:
        """Claim one batch and process it in order; on ``stop``, release what is left.

        A delivery whose lease ran out while earlier ones of the batch were processed is
        released, not processed: it gets its attempt back instead of being re-claimed (and
        charged an attempt it never ran) by another worker.
        """
        claimed = await self.claim()
        outcomes: list[Outcome] = []
        for index, delivery in enumerate(claimed):
            if stop is not None and stop.is_set():
                for remaining in claimed[index:]:
                    outcomes.append(await self.release(remaining))
                break
            if self._clock.now() >= delivery.lease_until:
                outcomes.append(await self.release(delivery))
                continue
            outcomes.append(await self.process(delivery))
        return outcomes

    async def refresh_stats(self) -> None:
        now = self._clock.now()
        async with UnitOfWork(self._session_factory) as uow:
            stats = await repository.delivery_stats(uow.session, now)
        self._metrics.update(stats, now)

    async def run(self, stop: asyncio.Event) -> None:
        """Loop until ``stop``: LISTEN wake-ups with a polling fallback, then drain and exit."""
        logger.info(
            "dispatcher.started",
            handlers=list(self._handlers.names),
            batch_size=self._config.batch_size,
            lease_seconds=self._config.lease.total_seconds(),
            listen=self._listener is not None,
        )
        next_stats = 0.0
        try:
            while not stop.is_set():
                processed = 0
                try:
                    processed = len(await self.run_once(stop))
                    if time.monotonic() >= next_stats:
                        await self.refresh_stats()
                        next_stats = time.monotonic() + self._config.stats_interval_seconds
                except Exception:
                    logger.exception("dispatcher.iteration_failed")
                if processed >= self._config.batch_size or stop.is_set():
                    continue  # a full batch: there is probably more work right now
                await self._wait(stop)
        finally:
            if self._listener is not None:
                await self._listener.close()
            logger.info("dispatcher.stopped")

    async def _wait(self, stop: asyncio.Event) -> None:
        timeout = self._config.poll_interval_seconds
        if self._listener is None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=timeout)
            return
        # notifies() returns within `timeout`, so shutdown waits at most one poll interval.
        await self._listener.wait(timeout)
