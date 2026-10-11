"""Dispatcher: leasing, fencing (F-22), inbox exactly-once, backoff, dead letters, replay."""

import asyncio
import dataclasses
import random
from datetime import timedelta

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from financial_hub.modules.events.application import (
    DeliveryStatus,
    Dispatcher,
    DispatcherConfig,
    Event,
    EventPublisher,
    Outcome,
    RetryPolicy,
    replay_delivery,
)
from financial_hub.modules.events.infrastructure.pg import PgListener, PgNotifier, engine_dsn
from financial_hub.platform.db import UnitOfWork
from financial_hub.shared.errors import Conflict, NotFound
from financial_hub.shared.ids import new_uuid
from tests.integration.events.support import AT, Harness, eventually


async def test_dispatches_each_delivery_once_with_its_effect(harness: Harness) -> None:
    handler = await harness.subscribed_handler()
    events = [await harness.publish(f"m{i}") for i in range(3)]
    dispatcher = harness.dispatcher()

    assert await dispatcher.run_once() == [Outcome.SUCCEEDED] * 3
    assert await dispatcher.run_once() == []

    assert [event_id for _, event_id in harness.calls] == [e.id for e in events]  # (txid, id)
    assert await harness.effects() == 3
    assert await harness.inbox_count(handler) == 3
    deliveries = await harness.deliveries()
    assert {(d["status"], d["attempts"], d["lease_token"]) for d in deliveries} == {
        ("succeeded", 1, None)
    }
    assert harness.metrics.total("events.deliveries.completed", outcome="succeeded") == 3
    attempts = harness.metrics.points("events.delivery.attempts")
    assert sum(point.count for point in attempts) == 3


async def test_handler_effect_rolls_back_with_its_failure(harness: Harness) -> None:
    handler = await harness.subscribed_handler(fail=lambda _e: True)
    await harness.publish()

    with structlog.testing.capture_logs() as logs:
        assert await harness.dispatcher().run_once() == [Outcome.RETRY_SCHEDULED]

    assert len(harness.calls) == 1
    assert await harness.effects() == 0  # written by the handler, then rolled back
    assert await harness.inbox_count(handler) == 0
    (delivery,) = await harness.deliveries()
    assert delivery["status"] == "retry_scheduled"
    assert delivery["last_error"].startswith("RuntimeError: boom")
    assert any(entry["event"] == "dispatcher.delivery_failed" for entry in logs)


async def test_inbox_makes_redelivery_a_no_op(harness: Harness) -> None:
    handler = await harness.subscribed_handler()
    await harness.publish()
    dispatcher = harness.dispatcher()
    assert await dispatcher.run_once() == [Outcome.SUCCEEDED]
    (delivery,) = await harness.deliveries()

    async with harness.uow() as uow:  # replay of a succeeded delivery (e.g. operator error)
        await replay_delivery(
            uow, project_id=harness.project_id, delivery_id=delivery["id"], clock=harness.clock
        )
    assert await dispatcher.run_once() == [Outcome.DUPLICATE]

    assert len(harness.calls) == 1
    assert await harness.effects() == 1
    assert await harness.inbox_count(handler) == 1
    assert (await harness.delivery(delivery["id"]))["status"] == "succeeded"


async def test_concurrent_dispatchers_process_each_delivery_exactly_once(harness: Harness) -> None:
    handler = await harness.subscribed_handler()
    for i in range(30):
        await harness.publish(f"m{i}")
    dispatchers = [harness.dispatcher(batch_size=4) for _ in range(4)]

    async def drain(dispatcher_index: int) -> int:
        done = 0
        while outcomes := await dispatchers[dispatcher_index].run_once():
            done += len(outcomes)
        return done

    totals = await asyncio.gather(*(drain(i) for i in range(4)))

    assert sum(totals) == 30
    assert sorted(event_id for _, event_id in harness.calls) == sorted(
        {event_id for _, event_id in harness.calls}
    )
    assert await harness.effects() == 30
    assert await harness.inbox_count(handler) == 30


async def test_stale_worker_after_lease_expiry_is_fenced(harness: Harness) -> None:
    """Worker A stalls past its lease; B re-leases and completes; A's late completion is lost."""
    await harness.subscribed_handler()
    await harness.publish()
    worker_a = harness.dispatcher(lease=timedelta(seconds=30))
    worker_b = harness.dispatcher(lease=timedelta(seconds=30))

    (stale,) = await worker_a.claim()
    assert stale.attempts == 1
    assert await worker_b.claim() == []  # lease still valid
    harness.clock.advance(timedelta(seconds=31))
    (fresh,) = await worker_b.claim()
    assert fresh.id == stale.id
    assert fresh.lease_token != stale.lease_token
    assert fresh.attempts == 2

    with structlog.testing.capture_logs() as logs:
        assert await worker_a.process(stale) == Outcome.FENCED
    assert harness.calls == []  # A never ran the handler: the CAS precedes it
    assert harness.metrics.total("dispatcher.fenced") == 1
    assert any(entry["event"] == "dispatcher.fenced" for entry in logs)

    assert await worker_b.process(fresh) == Outcome.SUCCEEDED
    assert await harness.effects() == 1
    assert (await harness.delivery(fresh.id))["status"] == "succeeded"
    assert await worker_a.process(stale) == Outcome.FENCED  # still fenced after completion
    assert harness.metrics.total("dispatcher.fenced") == 2


async def test_running_handler_keeps_its_delivery_locked(harness: Harness) -> None:
    """While A's handler runs, an expired lease cannot be stolen (the row is locked)."""
    started, release = asyncio.Event(), asyncio.Event()
    name, _ = await harness.subscribe()
    effect = harness.effect_handler(name)

    async def slow(event: Event, uow: UnitOfWork) -> None:
        started.set()
        await release.wait()
        await effect(event, uow)

    harness.handlers.register(name, slow)
    await harness.publish()
    worker_a, worker_b = harness.dispatcher(), harness.dispatcher()
    (claimed,) = await worker_a.claim()
    task = asyncio.create_task(worker_a.process(claimed))
    await asyncio.wait_for(started.wait(), timeout=5)

    harness.clock.advance(timedelta(minutes=5))
    assert await worker_b.claim() == []  # SKIP LOCKED: A holds the row
    release.set()
    assert await task == Outcome.SUCCEEDED
    assert await harness.effects() == 1


async def test_failure_completion_is_fenced_too(harness: Harness) -> None:
    await harness.subscribed_handler()
    await harness.publish()
    dispatcher = harness.dispatcher()
    (claimed,) = await dispatcher.claim()
    forged = dataclasses.replace(claimed, handler="test.unregistered", lease_token=new_uuid())

    assert await dispatcher.process(forged) == Outcome.FENCED
    (delivery,) = await harness.deliveries()
    assert (delivery["status"], delivery["lease_token"]) == ("leased", claimed.lease_token)


async def test_backoff_is_bounded_full_jitter_then_dead(harness: Harness) -> None:
    attempts_seen: list[int] = []

    def always_fail(_event: Event) -> bool:
        attempts_seen.append(len(attempts_seen) + 1)
        return True

    await harness.subscribed_handler(fail=always_fail)
    await harness.publish()
    policy = RetryPolicy(base_seconds=2, cap_seconds=10, max_attempts=5)
    rng = random.Random(7)  # noqa: S311 - seeded jitter, not crypto
    draws: list[float] = []

    def uniform() -> float:
        draws.append(rng.random())
        return draws[-1]

    dispatcher = harness.dispatcher(retry=policy, uniform=uniform)

    for attempt in range(1, 5):
        assert await dispatcher.run_once() == [Outcome.RETRY_SCHEDULED]
        (delivery,) = await harness.deliveries()
        delay = delivery["next_attempt_at"] - harness.clock.now()
        ceiling = timedelta(seconds=min(10, 2 * 2 ** (attempt - 1)))
        assert timedelta(0) <= delay <= ceiling
        assert delay == ceiling * draws[-1]
        assert delivery["attempts"] == attempt
        if delay > timedelta(0):
            harness.clock.set(delivery["next_attempt_at"] - timedelta(microseconds=1))
            assert await dispatcher.run_once() == []  # not due yet
        harness.clock.set(delivery["next_attempt_at"])

    with structlog.testing.capture_logs() as logs:
        assert await dispatcher.run_once() == [Outcome.DEAD]
    (dead,) = await harness.deliveries()
    assert (dead["status"], dead["attempts"]) == ("dead", 5)
    assert dead["last_error"].startswith("RuntimeError: boom")
    assert attempts_seen == [1, 2, 3, 4, 5]
    assert any(entry["event"] == "dispatcher.delivery_dead" for entry in logs)
    harness.clock.advance(timedelta(days=1))
    assert await dispatcher.run_once() == []  # dead stays dead
    assert harness.metrics.total("events.deliveries.completed", outcome="dead") == 1
    assert harness.metrics.total("events.deliveries.completed", outcome="retry_scheduled") == 4


async def test_replay_from_dead_delivers_again(harness: Harness) -> None:
    failing = True
    await harness.subscribed_handler(fail=lambda _e: failing)
    await harness.publish()
    dispatcher = harness.dispatcher(retry=RetryPolicy(max_attempts=1))
    assert await dispatcher.run_once() == [Outcome.DEAD]
    (delivery,) = await harness.deliveries()

    failing = False
    harness.clock.advance(timedelta(hours=1))
    async with harness.uow() as uow:
        await replay_delivery(
            uow, project_id=harness.project_id, delivery_id=delivery["id"], clock=harness.clock
        )
    replayed = await harness.delivery(delivery["id"])
    assert (replayed["status"], replayed["attempts"], replayed["last_error"]) == (
        "pending",
        0,
        None,
    )
    assert replayed["next_attempt_at"] == harness.clock.now()

    assert await dispatcher.run_once() == [Outcome.SUCCEEDED]
    assert await harness.effects() == 1


async def test_replay_rejects_unknown_and_active_deliveries(harness: Harness) -> None:
    await harness.subscribed_handler()
    await harness.publish()
    (delivery,) = await harness.deliveries()

    with pytest.raises(NotFound):
        async with harness.uow() as uow:
            await replay_delivery(
                uow, project_id=harness.project_id, delivery_id=new_uuid(), clock=harness.clock
            )
    with pytest.raises(NotFound):  # other project's id space
        async with harness.uow() as uow:
            await replay_delivery(
                uow, project_id=new_uuid(), delivery_id=delivery["id"], clock=harness.clock
            )
    with pytest.raises(Conflict, match="pending"):
        async with harness.uow() as uow:
            await replay_delivery(
                uow, project_id=harness.project_id, delivery_id=delivery["id"], clock=harness.clock
            )


async def test_crash_looping_delivery_goes_dead_without_running(harness: Harness) -> None:
    await harness.subscribed_handler()
    await harness.publish()
    dispatcher = harness.dispatcher(lease=timedelta(seconds=10), retry=RetryPolicy(max_attempts=2))

    for _ in range(2):  # the worker "crashes" after each claim
        assert len(await dispatcher.claim()) == 1
        harness.clock.advance(timedelta(seconds=11))
    (claimed,) = await dispatcher.claim()
    assert claimed.attempts == 3

    assert await dispatcher.process(claimed) == Outcome.DEAD
    assert harness.calls == []
    (delivery,) = await harness.deliveries()
    assert delivery["status"] == "dead"
    assert "lease expired" in delivery["last_error"]


async def test_handler_timeout_counts_as_a_failed_attempt(harness: Harness) -> None:
    name, _ = await harness.subscribe()

    async def hang(_event: Event, _uow: UnitOfWork) -> None:
        await asyncio.sleep(10)

    harness.handlers.register(name, hang)
    await harness.publish()

    outcomes = await harness.dispatcher(handler_timeout_seconds=0.05).run_once()

    assert outcomes == [Outcome.RETRY_SCHEDULED]
    (delivery,) = await harness.deliveries()
    assert delivery["last_error"].startswith("TimeoutError")


async def test_shutdown_releases_unprocessed_leases(harness: Harness) -> None:
    await harness.subscribed_handler()
    await harness.publish("a")
    await harness.publish("b")
    stop = asyncio.Event()
    stop.set()

    assert await harness.dispatcher().run_once(stop) == [Outcome.RELEASED, Outcome.RELEASED]

    deliveries = await harness.deliveries()
    assert {(d["status"], d["attempts"], d["lease_token"]) for d in deliveries} == {
        ("pending", 0, None)
    }
    assert harness.calls == []


async def test_batch_tail_with_an_expired_lease_is_released_not_charged(harness: Harness) -> None:
    """A slow head of the batch outlives the lease: the tail gets its attempt back."""
    name, _ = await harness.subscribe()

    async def slow(_event: Event, _uow: UnitOfWork) -> None:
        harness.clock.advance(timedelta(seconds=31))

    harness.handlers.register(name, slow)
    await harness.publish("a")
    await harness.publish("b")
    dispatcher = harness.dispatcher(lease=timedelta(seconds=30))

    assert await dispatcher.run_once() == [Outcome.SUCCEEDED, Outcome.RELEASED]
    statuses = sorted((d["status"], d["attempts"]) for d in await harness.deliveries())
    assert statuses == [("pending", 0), ("succeeded", 1)]

    assert await dispatcher.run_once() == [Outcome.SUCCEEDED]
    statuses = sorted((d["status"], d["attempts"]) for d in await harness.deliveries())
    assert statuses == [("succeeded", 1), ("succeeded", 1)]


async def test_deliveries_of_unknown_handlers_are_left_alone(harness: Harness) -> None:
    await harness.subscribe()  # nobody in this process handles it
    known = await harness.subscribed_handler()
    await harness.publish()

    assert await harness.dispatcher().run_once() == [Outcome.SUCCEEDED]
    statuses = {d["handler"]: d["status"] for d in await harness.deliveries()}
    assert statuses.pop(known) == "succeeded"
    assert list(statuses.values()) == ["pending"]


async def test_unregistered_handler_after_claim_is_retried(harness: Harness) -> None:
    await harness.subscribed_handler()
    await harness.publish()
    dispatcher = harness.dispatcher()
    (claimed,) = await dispatcher.claim()

    outcome = await dispatcher.process(dataclasses.replace(claimed, handler="test.gone"))

    assert outcome == Outcome.RETRY_SCHEDULED
    (delivery,) = await harness.deliveries()
    assert "not registered" in delivery["last_error"]


async def test_stats_feed_the_gauges(harness: Harness) -> None:
    await harness.subscribed_handler()
    await harness.publish()
    dispatcher = harness.dispatcher()
    harness.clock.advance(timedelta(seconds=90))

    await dispatcher.refresh_stats()

    lag = harness.metrics.points("events.delivery.lag")
    assert len(lag) == 1
    assert lag[0].value >= 90  # at least this test's delivery is that old
    backlog = {
        point.attributes["status"]: point.value
        for point in harness.metrics.points("events.deliveries")
    }
    assert set(backlog) == {"pending", "leased", "retry_scheduled", "dead"}
    assert backlog["pending"] >= 1
    (usage,) = harness.metrics.points("events.notification_queue.usage")
    assert 0 <= usage.value <= 1


async def test_run_loop_wakes_on_notify_and_stops_gracefully(
    app_engine: AsyncEngine, harness: Harness
) -> None:
    await harness.subscribed_handler()
    publisher = EventPublisher(clock=harness.clock, notifier=PgNotifier(app_engine))
    dispatcher = harness.dispatcher(poll_interval_seconds=0.5, stats_interval_seconds=0.1)
    dispatcher = Dispatcher(
        harness.factory,
        harness.handlers,
        clock=harness.clock,
        config=dispatcher.config,
        metrics=harness.metrics.events,
        listener=PgListener(engine_dsn(app_engine)),
    )
    stop = asyncio.Event()

    with structlog.testing.capture_logs() as logs:
        task = asyncio.create_task(dispatcher.run(stop))
        for i in range(3):
            async with harness.uow() as uow:
                await publisher.publish(uow, harness.draft(f"m{i}"))
        await eventually(harness.effects, lambda n: n >= 3)
        stop.set()
        await asyncio.wait_for(task, timeout=5)

    names = [entry["event"] for entry in logs]
    assert names[0] == "dispatcher.started"
    assert names[-1] == "dispatcher.stopped"
    assert await harness.effects() == 3


async def test_run_loop_polls_without_a_listener(harness: Harness) -> None:
    await harness.subscribed_handler()
    dispatcher = harness.dispatcher(poll_interval_seconds=0.05)
    stop = asyncio.Event()
    task = asyncio.create_task(dispatcher.run(stop))
    await harness.publish()
    await eventually(harness.effects, lambda n: n >= 1)
    stop.set()
    await asyncio.wait_for(task, timeout=5)


async def test_run_loop_survives_iteration_errors(harness: Harness) -> None:
    class Broken(Dispatcher):
        calls = 0

        async def run_once(self, stop: asyncio.Event | None = None) -> list[Outcome]:
            type(self).calls += 1
            if type(self).calls == 1:
                raise RuntimeError("database hiccup")
            stop_event.set()
            return []

    stop_event = asyncio.Event()
    dispatcher = Broken(
        harness.factory,
        harness.handlers,
        clock=harness.clock,
        config=DispatcherConfig(poll_interval_seconds=0.05),
    )
    with structlog.testing.capture_logs() as logs:
        await asyncio.wait_for(dispatcher.run(stop_event), timeout=10)
    assert Broken.calls == 2
    assert any(entry["event"] == "dispatcher.iteration_failed" for entry in logs)


def test_frozen_clock_starts_at_the_fixture_instant(harness: Harness) -> None:
    assert harness.clock.now() == AT
    assert DeliveryStatus.PENDING == "pending"
