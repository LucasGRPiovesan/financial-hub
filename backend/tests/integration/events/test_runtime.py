"""``build_runtime``: the worker's wiring of publisher, dispatcher and scheduler."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from financial_hub.modules.events.application import (
    Event,
    EventsSettings,
    HandlerRegistry,
    build_runtime,
)
from financial_hub.modules.events.infrastructure.pg import (
    AdvisoryLeaderLock,
    PgListener,
    PgNotifier,
)
from financial_hub.platform.db import UnitOfWork, create_session_factory
from financial_hub.shared.ids import new_uuid
from tests.integration.events.support import unique_handler


async def test_runtime_wires_postgres_plumbing_and_syncs_subscriptions(
    app_engine: AsyncEngine,
) -> None:
    handlers = HandlerRegistry()
    name = unique_handler()

    async def noop(_event: Event, _uow: UnitOfWork) -> None:
        return None

    handlers.register(name, noop, event_types=[f"test.rt_{new_uuid().hex[-8:]}"])
    runtime = build_runtime(
        app_engine,
        settings=EventsSettings(scheduler_lock_key=7, batch_size=5),  # pyright: ignore[reportCallIssue]
        handlers=handlers,
    )

    assert isinstance(runtime.publisher._notifier, PgNotifier)  # pyright: ignore[reportPrivateUsage]
    assert isinstance(runtime.dispatcher._listener, PgListener)  # pyright: ignore[reportPrivateUsage]
    assert isinstance(runtime.scheduler._leader, AdvisoryLeaderLock)  # pyright: ignore[reportPrivateUsage]
    assert runtime.dispatcher._config.batch_size == 5  # pyright: ignore[reportPrivateUsage]

    factory = create_session_factory(app_engine)
    async with UnitOfWork(factory) as uow:
        await runtime.sync_subscriptions(uow)
    async with UnitOfWork(factory) as uow:  # idempotent, then disabled: global subscriptions
        await runtime.sync_subscriptions(uow)  # must not outlive the test
        await uow.session.execute(
            text("UPDATE event_subscriptions SET enabled = false WHERE handler = :h"),
            {"h": name},
        )
