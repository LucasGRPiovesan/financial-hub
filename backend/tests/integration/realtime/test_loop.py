"""The broker loop: LISTEN wake-ups, polling fallback, reconnect backoff, failure recovery."""

import asyncio
from collections.abc import Callable
from typing import Any

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from financial_hub.modules.events.application.realtime import (
    RealtimeBroker,
    RealtimeConfig,
    build_broker,
)
from financial_hub.modules.events.infrastructure.listen import ListenWaker
from financial_hub.modules.events.infrastructure.pg import PgNotifier, engine_dsn
from tests.integration.realtime.support import ManualWaker, Rig, events_only, ids, prime

BAD_DSN = "postgresql://nobody:nothing@127.0.0.1:1/none?connect_timeout=1"


async def until(condition: Callable[[], bool], seconds: float = 5) -> None:
    async with asyncio.timeout(seconds):
        while not condition():  # noqa: ASYNC110
            await asyncio.sleep(0.01)


def loop_config(poll: float) -> RealtimeConfig:
    return RealtimeConfig(heartbeat_seconds=0.05, poll_interval_seconds=poll)


async def test_notify_wakes_the_broker_without_polling(rig: Rig) -> None:
    project = rig.project()
    waker = ListenWaker(engine_dsn(rig.app_engine))
    # A 30 s poll interval: only the NOTIFY can deliver inside the test's timeout.
    broker = RealtimeBroker(rig.factory, waker, config=loop_config(30), clock=rig.clock)
    await broker.start()
    try:
        stream = broker.stream(project.project_id)
        await prime(stream)
        await until(lambda: waker.connected)
        await asyncio.sleep(0.2)  # the loop is parked in wait(30)

        event = await project.publish("pushed")
        await PgNotifier(rig.app_engine).notify()

        assert ids(await events_only(stream, 1)) == [event.id]
        await stream.aclose()
    finally:
        await broker.stop()
    assert not waker.connected


async def test_polling_delivers_when_listen_is_unavailable(rig: Rig) -> None:
    project = rig.project()
    waker = ListenWaker(BAD_DSN, backoff_base_seconds=0.05, backoff_cap_seconds=0.1)
    broker = RealtimeBroker(rig.factory, waker, config=loop_config(0.05), clock=rig.clock)
    await broker.start()
    try:
        stream = broker.stream(project.project_id)
        await prime(stream)

        event = await project.publish("polled")  # no NOTIFY at all

        assert ids(await events_only(stream, 1)) == [event.id]
        assert not waker.connected
        await stream.aclose()
    finally:
        await broker.stop()


async def test_listen_reconnects_after_the_connection_is_killed(rig: Rig) -> None:
    now = [0.0]
    waker = ListenWaker(
        engine_dsn(rig.app_engine),
        backoff_base_seconds=1,
        backoff_cap_seconds=4,
        monotonic=lambda: now[0],
    )
    async with waker:
        assert await waker.wait(0.1) is True  # connecting counts: notifications may be missed
        assert await waker.wait(0.05) is False  # quiet: timeout
        async with rig.owner_engine.begin() as conn:
            await conn.execute(
                text("SELECT pg_terminate_backend(:pid)"), {"pid": waker.backend_pid}
            )

        assert await waker.wait(2) is False  # the dead connection is noticed
        assert not waker.connected
        assert waker.backend_pid is None
        assert await waker.wait(0.01) is False  # backing off: no reconnect yet
        assert not waker.connected
        now[0] += 1.0
        assert await waker.wait(0.1) is True  # reconnected
        assert waker.connected

        await PgNotifier(rig.app_engine).notify()
        assert await waker.wait(5) is True


async def test_reconnect_backoff_doubles_up_to_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[float] = []
    now = [0.0]

    async def refuse(*_args: Any, **_kwargs: Any) -> Any:
        attempts.append(now[0])
        raise psycopg.OperationalError("refused")

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", refuse)
    waker = ListenWaker(
        "postgresql://x/y", backoff_base_seconds=1, backoff_cap_seconds=3, monotonic=lambda: now[0]
    )
    for _ in range(20):
        assert await waker.wait(0.001) is False
        now[0] += 0.5

    # delays 1, 2, 3, 3...: attempts at t=0, 1, 3, 6, 9
    assert attempts[:5] == [0.0, 1.0, 3.0, 6.0, 9.0]


async def test_a_failing_listen_statement_is_a_failed_connect(rig: Rig) -> None:
    waker = ListenWaker(engine_dsn(rig.app_engine), channel="", backoff_base_seconds=0.01)

    assert await waker.wait(0.01) is False  # LISTEN "" is rejected by Postgres
    assert not waker.connected
    await waker.close()


class Flaky:
    """A session factory that fails its next ``failures`` calls."""

    def __init__(self, inner: async_sessionmaker[AsyncSession]) -> None:
        self.inner = inner
        self.failures = 0
        self.calls = 0

    def __call__(self) -> AsyncSession:
        self.calls += 1
        if self.failures:
            self.failures -= 1
            raise ConnectionError("database down")
        return self.inner()


async def test_broker_survives_database_failures(rig: Rig) -> None:
    project = rig.project()
    await project.publish("seed")  # a non-empty log, so the anchor has a cursor
    flaky = Flaky(rig.factory)
    flaky.failures = 2  # the first anchor attempts fail
    broker = RealtimeBroker(
        flaky,  # pyright: ignore[reportArgumentType]
        ManualWaker(),
        config=loop_config(0.02),
        clock=rig.clock,
    )
    await broker.start()
    await broker.start()  # idempotent
    try:
        await until(lambda: broker.cursor is not None)
        stream = broker.stream(project.project_id)
        await prime(stream)

        flaky.failures = 2  # now the pumps fail
        event = await project.publish("after the outage")

        assert ids(await events_only(stream, 1)) == [event.id]
        await stream.aclose()
    finally:
        await broker.stop()
        await broker.stop()  # idempotent


async def test_build_broker_reads_the_environment(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FH_EVENTS_STREAM_QUEUE_SIZE", "7")
    monkeypatch.setenv("FH_EVENTS_STREAM_REPLAY_MAX_EVENTS", "9")

    broker = build_broker(rig.app_engine)

    assert (broker.config.queue_size, broker.config.replay_max_events) == (7, 9)
    assert broker.config.heartbeat_seconds == 15
    assert broker.config.poll_interval_seconds == 2
    await broker.stop()
