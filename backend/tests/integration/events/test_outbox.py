"""Publishing: outbox atomicity, fan-out matching, post-commit NOTIFY, runtime privileges."""

import asyncio
from collections.abc import Awaitable, Callable

import pytest
import structlog
from opentelemetry.sdk.trace import TracerProvider
from psycopg import errors as pg
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from financial_hub.modules.events.application import (
    EventPublisher,
    current_traceparent,
    set_subscription_enabled,
)
from financial_hub.modules.events.infrastructure.pg import PgListener, PgNotifier, engine_dsn
from financial_hub.modules.events.registry import (
    EventPayload,
    EventRegistry,
    SystemTestPayload,
    UnregisteredEventType,
)
from financial_hub.shared.errors import ValidationFailed
from financial_hub.shared.ids import new_uuid
from tests.integration.events.support import AT, Harness, RecordingNotifier, unique_handler

type Build = Callable[[AsyncConnection], Awaitable[object]]


async def test_event_and_deliveries_commit_with_the_business_transaction(harness: Harness) -> None:
    handler, subscription_id = await harness.subscribe()

    event = await harness.publish("one", subject="customer:42")

    rows = await harness.rows("SELECT * FROM events WHERE project_id = :p")
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == event.id
    assert (row["type"], row["version"], row["subject"]) == ("system.test", 1, "customer:42")
    assert row["data"] == {"message": "one"}
    assert row["occurred_at"] == AT
    assert int(
        await harness.scalar("SELECT txid::text FROM events WHERE id = :id", id=event.id)
    ) == (event.txid)
    deliveries = await harness.deliveries()
    assert [(d["subscription_id"], d["status"], d["attempts"]) for d in deliveries] == [
        (subscription_id, "pending", 0)
    ]
    assert deliveries[0]["handler"] == handler
    assert deliveries[0]["next_attempt_at"] == AT
    assert event.public_id.startswith("evt_")


async def test_rollback_discards_event_and_deliveries(harness: Harness) -> None:
    await harness.subscribe()

    with pytest.raises(RuntimeError, match="business failed"):
        async with harness.uow() as uow:
            await harness.publisher.publish(uow, harness.draft("lost"))
            raise RuntimeError("business failed")

    assert await harness.events_count() == 0
    assert await harness.deliveries() == []


async def test_events_in_one_transaction_share_txid_and_metadata(harness: Harness) -> None:
    correlation, causation = new_uuid(), new_uuid()
    async with harness.uow() as uow:
        first = await harness.publisher.publish(uow, harness.draft("a"))
        second = await harness.publisher.publish(
            uow,
            harness.draft(
                "b",
                correlation_id=correlation,
                causation_id=causation,
                occurred_at=AT.replace(hour=1),
            ),
        )
    assert first.txid == second.txid
    row = (await harness.rows("SELECT * FROM events WHERE id = :id", id=second.id))[0]
    assert (row["correlation_id"], row["causation_id"]) == (correlation, causation)
    assert row["occurred_at"] == AT.replace(hour=1)


async def test_fan_out_matches_exact_prefix_and_wildcard_filters(harness: Harness) -> None:
    _, exact = await harness.subscribe(event_types=["system.test"])
    _, prefix = await harness.subscribe(event_types=["system.*"])
    _, wildcard = await harness.subscribe(event_types=["*"])
    _, other = await harness.subscribe(event_types=["ledger.*", "system.test_handled"])
    _, deeper = await harness.subscribe(event_types=["system.test.*"])
    _, foreign = await harness.subscribe(event_types=["*"], scope=new_uuid())

    await harness.publish()

    subscriptions = {d["subscription_id"] for d in await harness.deliveries()}
    assert subscriptions == {exact, prefix, wildcard}
    assert not subscriptions & {other, deeper, foreign}


async def test_global_subscription_receives_every_project(harness: Harness) -> None:
    registry = EventRegistry()
    event_type = f"test.global_{new_uuid().hex[-8:]}"
    registry.register(event_type, SystemTestPayload, description="global fan-out test")
    publisher = EventPublisher(clock=harness.clock, notifier=None, registry=registry)
    _, subscription_id = await harness.subscribe(event_types=[event_type], scope=None)
    other = new_uuid()
    try:
        for project in (harness.project_id, other):
            async with harness.uow() as uow:
                await publisher.publish(uow, harness.draft(type=event_type, project_id=project))
        count = await harness.scalar(
            "SELECT count(*) FROM event_deliveries WHERE subscription_id = :s", s=subscription_id
        )
        assert count == 2
    finally:  # a global subscription must not outlive its test
        async with harness.uow() as uow:
            await set_subscription_enabled(uow, subscription_id, False)


async def test_publish_validates_type_payload_and_subject(harness: Harness) -> None:
    with pytest.raises(UnregisteredEventType):
        await harness.publish(type="system.nope")
    with pytest.raises(UnregisteredEventType):
        await harness.publish(version=99)
    with pytest.raises(ValidationFailed) as info:
        await harness.publish(data={"message": 1, "extra": True})
    assert {error.pointer for error in info.value.errors} == {"/data/message", "/data/extra"}

    class Other(EventPayload):
        message: str

    with pytest.raises(ValidationFailed, match="expects SystemTestPayload"):
        await harness.publish(data=Other(message="x"))
    with pytest.raises(ValidationFailed, match="subject"):
        await harness.publish(subject="")
    with pytest.raises(ValidationFailed, match="subject"):
        await harness.publish(subject="x" * 256)
    assert await harness.events_count() == 0

    event = await harness.publish(data=SystemTestPayload(message="typed"))
    assert event.data == {"message": "typed"}


async def test_traceparent_of_the_active_span_is_recorded(harness: Harness) -> None:
    assert current_traceparent() is None
    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("publish") as span:
        event = await harness.publish()
        trace_id = format(span.get_span_context().trace_id, "032x")
    assert event.traceparent is not None
    assert trace_id in event.traceparent
    stored = await harness.scalar("SELECT traceparent FROM events WHERE id = :id", id=event.id)
    assert stored == event.traceparent


# --- NOTIFY ------------------------------------------------------------------------------------


async def test_notify_runs_once_after_commit_and_only_with_deliveries(harness: Harness) -> None:
    notifier = RecordingNotifier()
    publisher = EventPublisher(clock=harness.clock, notifier=notifier)

    async with harness.uow() as uow:
        await publisher.publish(uow, harness.draft())
    assert notifier.calls == 0  # no subscription: nothing to wake up

    await harness.subscribe()
    async with harness.uow() as uow:
        await publisher.publish(uow, harness.draft("a"))
        await publisher.publish(uow, harness.draft("b"))
        assert notifier.calls == 0  # never inside the business transaction
    assert notifier.calls == 1

    with pytest.raises(RuntimeError):
        async with harness.uow() as uow:
            await publisher.publish(uow, harness.draft("rolled back"))
            raise RuntimeError
    assert notifier.calls == 1


async def test_notify_failure_does_not_fail_the_commit(harness: Harness) -> None:
    notifier = RecordingNotifier(error=ConnectionError("db gone"))
    publisher = EventPublisher(
        clock=harness.clock, notifier=notifier, metrics=harness.metrics.events
    )
    await harness.subscribe()

    with structlog.testing.capture_logs() as logs:
        async with harness.uow() as uow:
            event = await publisher.publish(uow, harness.draft())

    assert notifier.calls == 1
    assert await harness.scalar("SELECT count(*) FROM events WHERE id = :id", id=event.id) == 1
    assert len(await harness.deliveries()) == 1
    assert harness.metrics.total("events.notify.failures") == 1
    assert any(
        entry["event"] == "events.notify_failed" and "db gone" in entry["error"] for entry in logs
    )


async def test_pg_notifier_wakes_a_listener(app_engine: AsyncEngine, harness: Harness) -> None:
    listener = PgListener(engine_dsn(app_engine))
    async with listener:
        assert await listener.wait(0.05) is False  # connects + LISTEN, nothing pending
        publisher = EventPublisher(clock=harness.clock, notifier=PgNotifier(app_engine))
        await harness.subscribe()
        async with harness.uow() as uow:
            await publisher.publish(uow, harness.draft())
        assert await asyncio.wait_for(listener.wait(5), timeout=10) is True


async def test_listener_survives_a_dead_connection(app_engine: AsyncEngine) -> None:
    listener = PgListener("postgresql://fh_app:wrong@db:5432/nowhere")
    with structlog.testing.capture_logs() as logs:
        assert await listener.wait(0.01) is False
    assert any(entry["event"] == "events.listen_failed" for entry in logs)
    await listener.close()


# --- fh_app privileges ---------------------------------------------------------------------------


async def statement_fails(engine: AsyncEngine, build: Build) -> str:
    async with engine.connect() as conn:
        await conn.begin()
        with pytest.raises(DBAPIError) as info:
            await build(conn)
        await conn.rollback()
    assert isinstance(info.value.orig, pg.InsufficientPrivilege), info.value.orig
    return str(info.value.orig)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE events SET data = '{}'::jsonb WHERE project_id = :p",
        "UPDATE events SET txid = '1'::xid8 WHERE project_id = :p",
        "DELETE FROM events WHERE project_id = :p",
        "TRUNCATE events",
        "DELETE FROM event_deliveries WHERE project_id = :p",
        "TRUNCATE event_deliveries",
        "DELETE FROM inbox WHERE handler = 'x'",
        "UPDATE inbox SET handler = 'x' WHERE handler = 'y'",
        "DELETE FROM event_subscriptions WHERE project_id = :p",
        "UPDATE event_subscriptions SET project_id = NULL WHERE project_id = :p",
        "UPDATE event_deliveries SET event_id = event_id WHERE project_id = :p",
        "DELETE FROM schedules WHERE project_id = :p",
        "DELETE FROM schedule_runs WHERE due_at < now()",
        "INSERT INTO events (id, type, version, project_id, occurred_at, data, txid) "
        "VALUES (gen_random_uuid(), 'system.test', 1, :p, now(), '{}', '1'::xid8)",
    ],
)
async def test_fh_app_cannot_rewrite_the_log(
    app_engine: AsyncEngine, harness: Harness, sql: str
) -> None:
    await harness.subscribe()
    await harness.publish()
    await statement_fails(
        app_engine, lambda conn: conn.execute(text(sql), {"p": harness.project_id})
    )


async def test_tests_run_as_fh_app(app_engine: AsyncEngine) -> None:
    async with app_engine.connect() as conn:
        assert (await conn.execute(text("SELECT current_user"))).scalar_one() == "fh_app"


async def test_unknown_handler_names_are_rejected(harness: Harness) -> None:
    with pytest.raises(ValidationFailed, match="invalid handler name"):
        await harness.subscribe("Bad Name")
    with pytest.raises(ValidationFailed, match="invalid event type filter"):
        await harness.subscribe(event_types=["system."])
    with pytest.raises(ValidationFailed, match="at least one"):
        await harness.subscribe(event_types=[])
    assert unique_handler() != unique_handler()
