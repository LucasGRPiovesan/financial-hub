"""Interval scheduler (ADR-0005): emits ``<type>`` events when a schedule falls due.

One leader at a time holds a session advisory lock (``pg_try_advisory_lock``); followers keep
trying every tick and take over when the leader's connection dies. Leadership is an efficiency
measure, not the correctness guarantee: each schedule is locked ``FOR UPDATE SKIP LOCKED`` in
its own transaction, a run is emitted at most once per ``(schedule, due_at)``
(``schedule_runs`` primary key, checked first), and the event, the run row and the advanced
``next_due_at`` commit together. Missed instants (downtime) are emitted on catch-up, at most
``catch_up_limit`` per schedule per tick. Only fixed intervals for now; cron expressions
would need a new dependency (deferred).
"""

import asyncio
import contextlib
from datetime import timedelta
from typing import Final

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from financial_hub.modules.events.application.publisher import EventDraft, EventPublisher
from financial_hub.modules.events.domain import due_runs
from financial_hub.modules.events.infrastructure import repository
from financial_hub.modules.events.infrastructure.pg import AdvisoryLeaderLock
from financial_hub.modules.events.registry import ScheduleDuePayload
from financial_hub.platform.db import UnitOfWork
from financial_hub.shared.clock import Clock

LEADER_LOCK_CLASS: Final = 0x46480006
"""Advisory-lock class of scheduler leadership (the key comes from the settings)."""

logger = structlog.get_logger()


class Scheduler:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        publisher: EventPublisher,
        *,
        clock: Clock,
        leader: AdvisoryLeaderLock | None = None,
        tick_seconds: float = 5.0,
        catch_up_limit: int = 100,
        batch_size: int = 100,
    ) -> None:
        self._session_factory = session_factory
        self._publisher = publisher
        self._clock = clock
        self._leader = leader
        self._tick_seconds = tick_seconds
        self._catch_up_limit = catch_up_limit
        self._batch_size = batch_size

    async def tick(self) -> int:
        """Emit every due run; returns how many events were published."""
        now = self._clock.now()
        async with UnitOfWork(self._session_factory) as uow:
            schedule_ids = await repository.due_schedule_ids(uow.session, now, self._batch_size)
        emitted = 0
        for schedule_id in schedule_ids:
            async with UnitOfWork(self._session_factory) as uow:
                session = uow.session
                schedule = await repository.lock_due_schedule(session, schedule_id, now)
                if schedule is None:  # another scheduler has it, or it is no longer due
                    continue
                runs, next_due_at = due_runs(
                    schedule.next_due_at,
                    timedelta(seconds=schedule.interval_seconds),
                    now,
                    self._catch_up_limit,
                )
                for due_at in runs:
                    if await repository.schedule_run_exists(session, schedule.id, due_at):
                        continue
                    event = await self._publisher.publish(
                        uow,
                        EventDraft(
                            type=schedule.event_type,
                            data=ScheduleDuePayload(schedule=schedule.name, due_at=due_at),
                            project_id=schedule.project_id,
                            subject=f"schedule:{schedule.name}",
                            occurred_at=due_at,
                        ),
                    )
                    await repository.insert_schedule_run(session, schedule.id, due_at, event.id)
                    emitted += 1
                await repository.advance_schedule(session, schedule.id, next_due_at)
        return emitted

    async def run(self, stop: asyncio.Event) -> None:
        """Tick while leader; followers retry leadership every tick. Releases it on ``stop``."""
        logger.info("scheduler.started", tick_seconds=self._tick_seconds)
        leading = False
        try:
            while not stop.is_set():
                try:
                    leading = await self._lead(leading)
                    if leading:
                        emitted = await self.tick()
                        if emitted:
                            logger.info("scheduler.runs_emitted", count=emitted)
                except Exception:
                    logger.exception("scheduler.tick_failed")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self._tick_seconds)
        finally:
            if self._leader is not None:
                await self._leader.release()
            logger.info("scheduler.stopped")

    async def _lead(self, leading: bool) -> bool:
        if self._leader is None:
            return True
        if leading and await self._leader.held():
            return True
        if leading:
            logger.warning("scheduler.leadership_lost")
        if await self._leader.try_acquire():
            logger.info("scheduler.leadership_acquired")
            return True
        return False
