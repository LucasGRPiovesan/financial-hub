"""Shared helpers for event log tests against real Postgres (as ``fh_app``).

Isolation: nothing can be deleted, so each test uses a fresh ``project_id`` and unique handler
names (``test.h<hex>``). The dispatcher only claims deliveries of handlers it knows, so the
compose ``worker`` (empty handler registry) never steals test deliveries, and test dispatchers
never touch anyone else's. Schedules live in 2099 so the real scheduler never finds them due.
"""

import asyncio
import os
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from financial_hub.modules.events.application import (
    Dispatcher,
    DispatcherConfig,
    Event,
    EventDraft,
    EventPublisher,
    EventsMetrics,
    HandlerRegistry,
    Notifier,
    RetryPolicy,
    ensure_internal_subscription,
)
from financial_hub.modules.events.registry import SYSTEM_TEST, SYSTEM_TEST_HANDLED
from financial_hub.platform.db import UnitOfWork
from financial_hub.shared.clock import FrozenClock
from financial_hub.shared.ids import new_uuid

APP_URL_ENV = "FH_APP_TEST_DATABASE_URL"
AT = datetime(2026, 10, 10, 12, tzinfo=UTC)


def app_database_url() -> str:
    url = os.environ.get(APP_URL_ENV)
    if not url:
        pytest.fail(f"{APP_URL_ENV} is not set: run the suite with scripts/test.sh")
    return url


def make_app_engine(pool_size: int = 10) -> AsyncEngine:
    return create_async_engine(app_database_url(), pool_size=pool_size, max_overflow=10)


def unique_handler() -> str:
    return f"test.h{new_uuid().hex[-12:]}"


class RecordingNotifier:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls = 0
        self.error = error

    async def notify(self) -> None:
        self.calls += 1
        if self.error is not None:
            raise self.error


class Metrics:
    """``EventsMetrics`` on a private SDK provider, readable in tests."""

    def __init__(self) -> None:
        self.reader = InMemoryMetricReader()
        self.provider = MeterProvider(metric_readers=[self.reader])
        self.events = EventsMetrics(self.provider.get_meter("test"))

    def points(self, name: str) -> list[Any]:
        data = self.reader.get_metrics_data()
        if data is None:
            return []
        return [
            point
            for resource in data.resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
            if metric.name == name
            for point in metric.data.data_points
        ]

    def total(self, name: str, **attributes: str) -> float:
        return sum(
            point.value
            for point in self.points(name)
            if all(point.attributes.get(k) == v for k, v in attributes.items())
        )


class Harness:
    """One project's view of the event log: publish, subscribe, dispatch, inspect."""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        owner_engine: AsyncEngine,
        project_id: UUID,
        *,
        notifier: Notifier | None = None,
    ) -> None:
        self.factory = factory
        self.owner_engine = owner_engine
        self.project_id = project_id
        self.clock = FrozenClock(AT)
        self.metrics = Metrics()
        self.publisher = EventPublisher(
            clock=self.clock, notifier=notifier, metrics=self.metrics.events
        )
        self.handlers = HandlerRegistry()
        self.calls: list[tuple[str, UUID]] = []

    def uow(self) -> UnitOfWork:
        return UnitOfWork(self.factory)

    async def publish(self, message: str = "hello", **overrides: Any) -> Event:
        async with self.uow() as uow:
            return await self.publisher.publish(uow, self.draft(message, **overrides))

    def draft(self, message: str = "hello", **overrides: Any) -> EventDraft:
        values: dict[str, Any] = {
            "type": SYSTEM_TEST.name,
            "data": {"message": message},
            "project_id": self.project_id,
        }
        values.update(overrides)
        return EventDraft(**values)

    async def subscribe(
        self,
        handler: str | None = None,
        event_types: Sequence[str] = ("system.test",),
        *,
        scope: UUID | Literal["project"] | None = "project",
    ) -> tuple[str, UUID]:
        name = handler or unique_handler()
        async with self.uow() as uow:
            subscription_id = await ensure_internal_subscription(
                uow,
                handler=name,
                event_types=event_types,
                project_id=self.project_id if scope == "project" else scope,
            )
        return name, subscription_id

    def effect_handler(
        self, name: str, *, fail: Callable[[Event], bool] = lambda _e: False
    ) -> Callable[[Event, UnitOfWork], Awaitable[None]]:
        """Handler whose effect is a ``system.test_handled`` event (``causation_id`` = input)."""

        async def handle(event: Event, uow: UnitOfWork) -> None:
            self.calls.append((name, event.id))
            await self.publisher.publish(
                uow,
                EventDraft(
                    type=SYSTEM_TEST_HANDLED.name,
                    data={"message": f"{name}:{event.id}"},
                    project_id=event.project_id,
                    causation_id=event.id,
                ),
            )
            if fail(event):
                raise RuntimeError(f"boom {event.id}")

        return handle

    async def subscribed_handler(self, **kwargs: Any) -> str:
        name, _ = await self.subscribe()
        self.handlers.register(name, self.effect_handler(name, **kwargs))
        return name

    def dispatcher(
        self,
        *,
        lease: timedelta = timedelta(seconds=60),
        retry: RetryPolicy | None = None,
        uniform: Callable[[], float] = lambda: 0.5,
        handlers: HandlerRegistry | None = None,
        **config: Any,
    ) -> Dispatcher:
        return Dispatcher(
            self.factory,
            handlers or self.handlers,
            clock=self.clock,
            config=DispatcherConfig(lease=lease, retry=retry or RetryPolicy(), **config),
            metrics=self.metrics.events,
            uniform=uniform,
        )

    # --- inspection (owner connection: reads are not what is under test) ----------------------

    async def scalar(self, sql: str, **params: Any) -> Any:
        async with self.owner_engine.connect() as conn:
            return (await conn.execute(text(sql), {"p": self.project_id, **params})).scalar_one()

    async def rows(self, sql: str, **params: Any) -> list[dict[str, Any]]:
        async with self.owner_engine.connect() as conn:
            result = await conn.execute(text(sql), {"p": self.project_id, **params})
            return [dict(row) for row in result.mappings()]

    async def effects(self) -> int:
        return await self.scalar(
            "SELECT count(*) FROM events WHERE project_id = :p AND type = 'system.test_handled'"
        )

    async def events_count(self) -> int:
        return await self.scalar("SELECT count(*) FROM events WHERE project_id = :p")

    async def deliveries(self) -> list[dict[str, Any]]:
        return await self.rows(
            "SELECT d.*, s.handler FROM event_deliveries d "
            "JOIN event_subscriptions s ON s.id = d.subscription_id "
            "WHERE d.project_id = :p ORDER BY d.created_at, d.id"
        )

    async def delivery(self, delivery_id: UUID) -> dict[str, Any]:
        rows = await self.rows(
            "SELECT * FROM event_deliveries WHERE id = :id AND project_id = :p", id=delivery_id
        )
        assert len(rows) == 1
        return rows[0]

    async def inbox_count(self, handler: str) -> int:
        return await self.scalar("SELECT count(*) FROM inbox WHERE handler = :h", h=handler)


async def eventually[T](
    probe: Callable[[], Awaitable[T]], until: Callable[[T], bool], seconds: float = 10
) -> T:
    """Poll ``probe`` until ``until(value)`` holds; the test fails after ``seconds``."""
    async with asyncio.timeout(seconds):
        while True:
            value = await probe()
            if until(value):
                return value
            await asyncio.sleep(0.02)
