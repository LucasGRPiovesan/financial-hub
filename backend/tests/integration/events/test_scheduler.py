"""Scheduler: leadership, idempotent runs per (schedule, due_at), catch-up, concurrency."""

import asyncio
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from financial_hub.modules.events.application import (
    LEADER_LOCK_CLASS,
    EventPublisher,
    Scheduler,
    upsert_schedule,
)
from financial_hub.modules.events.infrastructure.pg import AdvisoryLeaderLock, engine_dsn
from financial_hub.modules.events.registry import UnregisteredEventType
from financial_hub.shared.errors import ValidationFailed
from tests.integration.events.support import Harness, eventually

FAR = datetime(2099, 1, 1, tzinfo=UTC)


def leader(app_engine: AsyncEngine, key: int) -> AdvisoryLeaderLock:
    return AdvisoryLeaderLock(engine_dsn(app_engine), LEADER_LOCK_CLASS, key)


def scheduler(harness: Harness, **kwargs: object) -> Scheduler:
    publisher = EventPublisher(clock=harness.clock, notifier=None)
    return Scheduler(harness.factory, publisher, clock=harness.clock, **kwargs)  # pyright: ignore[reportArgumentType]


async def schedule(
    harness: Harness,
    name: str = "nightly",
    *,
    interval: int = 60,
    due: datetime = FAR,
    enabled: bool = True,
) -> UUID:
    async with harness.uow() as uow:
        return await upsert_schedule(
            uow,
            project_id=harness.project_id,
            name=name,
            event_type="system.test_schedule_due",
            interval_seconds=interval,
            next_due_at=due,
            enabled=enabled,
        )


async def runs(harness: Harness, schedule_id: UUID) -> list[datetime]:
    rows = await harness.rows(
        "SELECT due_at FROM schedule_runs WHERE schedule_id = :s ORDER BY due_at", s=schedule_id
    )
    return [row["due_at"] for row in rows]


async def scheduled_events(harness: Harness) -> list[dict[str, object]]:
    return await harness.rows(
        "SELECT data, subject, occurred_at FROM events "
        "WHERE project_id = :p AND type = 'system.test_schedule_due' ORDER BY occurred_at"
    )


@pytest.fixture(autouse=True)
async def disable_schedules(harness: Harness):
    """Leave no due schedule behind: later tests tick the (global) schedule table too."""
    yield
    async with harness.owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE schedules SET enabled = false WHERE project_id = :p"),
            {"p": harness.project_id},
        )


async def test_emits_each_due_run_once_and_advances(harness: Harness) -> None:
    schedule_id = await schedule(harness)
    harness.clock.set(FAR + timedelta(minutes=2, seconds=30))
    tick = scheduler(harness)

    await tick.tick()
    await tick.tick()  # nothing new is due

    expected = [FAR, FAR + timedelta(minutes=1), FAR + timedelta(minutes=2)]
    assert await runs(harness, schedule_id) == expected
    events = await scheduled_events(harness)
    assert [e["occurred_at"] for e in events] == expected
    assert events[0]["data"] == {"schedule": "nightly", "due_at": "2099-01-01T00:00:00Z"}
    assert events[0]["subject"] == "schedule:nightly"
    next_due = await harness.scalar(
        "SELECT next_due_at FROM schedules WHERE id = :s", s=schedule_id
    )
    assert next_due == FAR + timedelta(minutes=3)


async def test_catch_up_is_bounded_per_tick(harness: Harness) -> None:
    schedule_id = await schedule(harness, interval=10)
    harness.clock.set(FAR + timedelta(seconds=45))  # 5 runs due: 0, 10, 20, 30, 40
    tick = scheduler(harness, catch_up_limit=2)

    await tick.tick()
    assert len(await runs(harness, schedule_id)) == 2
    await tick.tick()
    await tick.tick()
    assert len(await runs(harness, schedule_id)) == 5
    await tick.tick()
    assert len(await runs(harness, schedule_id)) == 5


async def test_a_run_is_never_emitted_twice(harness: Harness) -> None:
    schedule_id = await schedule(harness)
    harness.clock.set(FAR + timedelta(seconds=1))
    await scheduler(harness).tick()
    await schedule(harness, due=FAR)  # rewind next_due_at (e.g. a config re-apply)
    harness.clock.set(FAR + timedelta(minutes=1, seconds=1))

    await scheduler(harness).tick()

    assert await runs(harness, schedule_id) == [FAR, FAR + timedelta(minutes=1)]
    assert len(await scheduled_events(harness)) == 2


async def test_disabled_or_future_schedules_do_not_fire(harness: Harness) -> None:
    await schedule(harness, "off", enabled=False)
    await schedule(harness, "later", due=FAR + timedelta(days=1))
    harness.clock.set(FAR + timedelta(hours=1))
    await scheduler(harness).tick()
    assert await scheduled_events(harness) == []


async def test_concurrent_schedulers_emit_each_run_exactly_once(harness: Harness) -> None:
    ids = [await schedule(harness, f"s{i}", interval=30) for i in range(6)]
    harness.clock.set(FAR + timedelta(seconds=95))  # 4 runs each: 0, 30, 60, 90
    schedulers = [scheduler(harness) for _ in range(4)]

    await asyncio.gather(*(s.tick() for s in schedulers))
    await asyncio.gather(*(s.tick() for s in schedulers))

    for schedule_id in ids:
        assert len(await runs(harness, schedule_id)) == 4
    assert len(await scheduled_events(harness)) == 24


async def test_only_one_leader_at_a_time(app_engine: AsyncEngine) -> None:
    key = secrets.randbelow(2**31 - 2) + 2  # never the worker's key (1)
    first, second = leader(app_engine, key), leader(app_engine, key)
    try:
        assert await first.try_acquire() is True
        assert await first.try_acquire() is True  # re-entrant for the holder
        assert await second.try_acquire() is False
        assert await first.held() is True
        assert second.is_leader is False
        await first.release()
        assert await first.held() is False
        assert await second.try_acquire() is True
    finally:
        await first.release()
        await second.release()


async def test_leadership_moves_when_the_leader_connection_dies(
    app_engine: AsyncEngine, owner_engine: AsyncEngine
) -> None:
    key = secrets.randbelow(2**31 - 2) + 2
    first, second = leader(app_engine, key), leader(app_engine, key)
    try:
        assert await first.try_acquire() is True
        pid = first._connection.info.backend_pid  # pyright: ignore[reportOptionalMemberAccess, reportPrivateUsage]
        async with owner_engine.begin() as conn:
            await conn.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
        with structlog.testing.capture_logs() as logs:
            assert await first.held() is False
        assert any(entry["event"] == "scheduler.leadership_lost" for entry in logs)
        assert await second.try_acquire() is True
    finally:
        await first.release()
        await second.release()


async def test_run_loop_leads_ticks_and_releases(app_engine: AsyncEngine, harness: Harness) -> None:
    schedule_id = await schedule(harness)
    harness.clock.set(FAR + timedelta(seconds=1))
    key = secrets.randbelow(2**31 - 2) + 2
    lock = leader(app_engine, key)
    follower_lock = leader(app_engine, key)
    stop, follower_stop = asyncio.Event(), asyncio.Event()
    leading = scheduler(harness, leader=lock, tick_seconds=0.05)
    follower = scheduler(harness, leader=follower_lock, tick_seconds=0.05)

    async def follower_leads() -> bool:
        return follower_lock.is_leader

    with structlog.testing.capture_logs() as logs:
        task = asyncio.create_task(leading.run(stop))
        await eventually(lambda: runs(harness, schedule_id), bool)
        follower_task = asyncio.create_task(follower.run(follower_stop))
        await asyncio.sleep(0.2)
        assert follower_lock.is_leader is False
        stop.set()
        await asyncio.wait_for(task, timeout=5)
        await eventually(follower_leads, bool)  # takes over once the leader is gone
        follower_stop.set()
        await asyncio.wait_for(follower_task, timeout=5)

    names = [entry["event"] for entry in logs]
    assert names.count("scheduler.started") == 2
    assert names.count("scheduler.leadership_acquired") == 2
    assert names.count("scheduler.stopped") == 2
    assert "scheduler.runs_emitted" in names
    assert await runs(harness, schedule_id) == [FAR]


async def test_run_loop_without_lock_and_with_tick_errors(harness: Harness) -> None:
    class Broken(Scheduler):
        async def tick(self) -> int:
            stop.set()
            raise RuntimeError("boom")

    stop = asyncio.Event()
    broken = Broken(
        harness.factory,
        EventPublisher(clock=harness.clock, notifier=None),
        clock=harness.clock,
        tick_seconds=0.01,
    )
    with structlog.testing.capture_logs() as logs:
        await asyncio.wait_for(broken.run(stop), timeout=5)
    assert any(entry["event"] == "scheduler.tick_failed" for entry in logs)


async def test_upsert_schedule_validates(harness: Harness) -> None:
    with pytest.raises(ValidationFailed, match="ScheduleDuePayload"):
        async with harness.uow() as uow:
            await upsert_schedule(
                uow,
                project_id=harness.project_id,
                name="x",
                event_type="system.test",
                interval_seconds=60,
                next_due_at=FAR,
            )
    with pytest.raises(UnregisteredEventType):
        await schedule_with(harness, event_type="system.missing")
    with pytest.raises(ValidationFailed, match="schedule name"):
        await schedule_with(harness, name="Bad Name")
    with pytest.raises(ValidationFailed, match="interval"):
        await schedule_with(harness, interval_seconds=0)
    with pytest.raises(ValidationFailed, match="timezone"):
        await schedule_with(harness, next_due_at=datetime(2099, 1, 1))


async def schedule_with(harness: Harness, **overrides: object) -> UUID:
    values: dict[str, object] = {
        "project_id": harness.project_id,
        "name": "valid",
        "event_type": "system.test_schedule_due",
        "interval_seconds": 60,
        "next_due_at": FAR,
    }
    values.update(overrides)
    async with harness.uow() as uow:
        return await upsert_schedule(uow, **values)  # pyright: ignore[reportArgumentType]
