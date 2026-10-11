"""Wiring of the publisher, dispatcher and scheduler for one process (used by the worker)."""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from financial_hub.modules.events.application.config import EventsSettings
from financial_hub.modules.events.application.dispatcher import Dispatcher, DispatcherConfig
from financial_hub.modules.events.application.handlers import HandlerRegistry
from financial_hub.modules.events.application.handlers import handlers as canonical_handlers
from financial_hub.modules.events.application.metrics import EventsMetrics
from financial_hub.modules.events.application.publisher import EventPublisher
from financial_hub.modules.events.application.scheduler import LEADER_LOCK_CLASS, Scheduler
from financial_hub.modules.events.application.service import sync_handler_subscriptions
from financial_hub.modules.events.infrastructure.pg import (
    AdvisoryLeaderLock,
    PgListener,
    PgNotifier,
    engine_dsn,
)
from financial_hub.platform.db import UnitOfWork, create_session_factory
from financial_hub.shared.clock import Clock, SystemClock


@dataclass(frozen=True, slots=True)
class EventsRuntime:
    publisher: EventPublisher
    dispatcher: Dispatcher
    scheduler: Scheduler
    handlers: HandlerRegistry

    async def sync_subscriptions(self, uow: UnitOfWork) -> None:
        await sync_handler_subscriptions(uow, self.handlers)


def build_runtime(
    engine: AsyncEngine,
    *,
    settings: EventsSettings | None = None,
    clock: Clock | None = None,
    handlers: HandlerRegistry = canonical_handlers,
    metrics: EventsMetrics | None = None,
) -> EventsRuntime:
    settings = settings or EventsSettings()
    clock = clock or SystemClock()
    metrics = metrics or EventsMetrics()
    session_factory = create_session_factory(engine)
    dsn = engine_dsn(engine)
    publisher = EventPublisher(clock=clock, notifier=PgNotifier(engine), metrics=metrics)
    dispatcher = Dispatcher(
        session_factory,
        handlers,
        clock=clock,
        config=DispatcherConfig(
            batch_size=settings.batch_size,
            lease=settings.lease,
            handler_timeout_seconds=settings.handler_timeout_seconds,
            poll_interval_seconds=settings.poll_interval_seconds,
            stats_interval_seconds=settings.stats_interval_seconds,
            retry=settings.retry_policy,
        ),
        metrics=metrics,
        listener=PgListener(dsn),
    )
    scheduler = Scheduler(
        session_factory,
        publisher,
        clock=clock,
        leader=AdvisoryLeaderLock(dsn, LEADER_LOCK_CLASS, settings.scheduler_lock_key),
        tick_seconds=settings.scheduler_tick_seconds,
        catch_up_limit=settings.scheduler_catch_up_limit,
    )
    return EventsRuntime(publisher, dispatcher, scheduler, handlers)
