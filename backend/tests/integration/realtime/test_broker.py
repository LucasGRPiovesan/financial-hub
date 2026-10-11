"""Realtime broker and stream: live delivery, replay, isolation, heartbeat, overflow, resync."""

import asyncio
from datetime import timedelta
from uuid import UUID

import pytest

from financial_hub.modules.events.application import Event
from financial_hub.modules.events.application.realtime import (
    RealtimeConfig,
    ResyncMarker,
    Subscription,
)
from financial_hub.modules.events.domain import EventCursor
from financial_hub.shared.ids import new_uuid
from tests.integration.events.support import AT
from tests.integration.realtime.support import (
    FAST,
    Rig,
    cursor_of,
    events_only,
    ids,
    prime,
    publish_many,
    pump_to,
)


def fake_event(project_id: UUID) -> Event:
    return Event(
        id=new_uuid(),
        type="system.test",
        version=1,
        project_id=project_id,
        subject=None,
        occurred_at=AT,
        correlation_id=None,
        causation_id=None,
        traceparent=None,
        data={},
        txid=1,
    )


async def test_live_event_is_delivered(rig: Rig) -> None:
    project = rig.project()
    broker = await rig.broker()
    stream = broker.stream(project.project_id)
    await prime(stream)

    event = await project.publish("hello")
    await pump_to(broker, event)
    (message,) = await events_only(stream, 1)

    assert message.kind == "event"
    assert message.event is not None
    assert (message.event.id, message.event.type) == (event.id, "system.test")
    assert message.event.data == {"message": "hello"}
    await stream.aclose()


async def test_events_arrive_in_commit_order(rig: Rig) -> None:
    project = rig.project()
    broker = await rig.broker()
    stream = broker.stream(project.project_id)
    await prime(stream)

    published = await publish_many(project, 5)
    await pump_to(broker, *published)

    assert ids(await events_only(stream, 5)) == [e.id for e in published]
    await stream.aclose()


async def test_history_is_not_replayed_without_last_event_id(rig: Rig) -> None:
    project = rig.project()
    before = await project.publish("before")
    broker = await rig.broker()
    stream = broker.stream(project.project_id)
    await prime(stream)

    after = await project.publish("after")
    await pump_to(broker, before, after)

    assert ids(await events_only(stream, 1)) == [after.id]
    await stream.aclose()


async def test_reconnect_replays_missed_events_exactly_once_and_in_order(rig: Rig) -> None:
    project = rig.project()
    broker = await rig.broker()
    first_connection = broker.stream(project.project_id)
    await prime(first_connection)
    seen = await project.publish("seen")
    await pump_to(broker, seen)
    (delivered,) = await events_only(first_connection, 1)
    assert delivered.event is not None
    await first_connection.aclose()
    assert broker.subscriber_count == 0

    # Missed while disconnected: the broker has not read them yet, so after reconnecting they
    # reach the new connection twice (replay and live queue) and must be delivered once.
    missed = await publish_many(project, 3)
    reconnected = broker.stream(project.project_id, cursor_of(delivered.event))
    replayed = await events_only(reconnected, 3)
    await pump_to(broker, *missed)
    live = await project.publish("live")
    await pump_to(broker, live)
    rest = await events_only(reconnected, 1)

    assert ids(replayed) == [e.id for e in missed]
    assert ids(rest) == [live.id]
    await reconnected.aclose()


async def test_replay_when_nothing_was_missed_just_goes_live(rig: Rig) -> None:
    project = rig.project()
    broker = await rig.broker()
    seen = await project.publish("seen")
    await pump_to(broker, seen)

    stream = broker.stream(project.project_id, cursor_of(seen))
    await prime(stream)
    live = await project.publish("live")
    await pump_to(broker, live)

    assert ids(await events_only(stream, 1)) == [live.id]
    await stream.aclose()


async def test_projects_are_isolated(rig: Rig) -> None:
    mine, other = rig.project(), rig.project()
    broker = await rig.broker()
    stream = broker.stream(mine.project_id)
    await prime(stream)

    foreign = await other.publish("not for you")
    own = await mine.publish("for you")
    await pump_to(broker, foreign, own)

    assert ids(await events_only(stream, 1)) == [own.id]
    # nothing else follows: only heartbeats
    async with asyncio.timeout(1):
        assert (await anext(stream)).kind == "heartbeat"
    await stream.aclose()


async def test_another_projects_cursor_cannot_be_used_to_replay(rig: Rig) -> None:
    mine, other = rig.project(), rig.project()
    foreign = await other.publish("secret")
    await other.publish("secret too")
    broker = await rig.broker()

    stream = broker.stream(mine.project_id, cursor_of(foreign))

    assert (await anext(stream)).kind == "resync"
    await stream.aclose()


async def test_heartbeat_is_sent_when_idle(rig: Rig) -> None:
    broker = await rig.broker()
    stream = broker.stream(rig.project().project_id)

    async with asyncio.timeout(1):
        messages = [await anext(stream) for _ in range(2)]

    assert [m.kind for m in messages] == ["heartbeat", "heartbeat"]
    await stream.aclose()


async def test_queue_overflow_drops_the_backlog_and_signals_resync(rig: Rig) -> None:
    project = rig.project()
    broker = await rig.broker(RealtimeConfig(heartbeat_seconds=0.05, queue_size=2))
    stream = broker.stream(project.project_id)
    await prime(stream)

    burst = await publish_many(project, 4)  # a slow consumer: nobody reads meanwhile
    await pump_to(broker, *burst)
    (resync,) = await events_only(stream, 1)
    after = await project.publish("after")
    await pump_to(broker, after)

    assert resync.kind == "resync"
    assert resync.event is None
    assert ids(await events_only(stream, 1)) == [after.id]  # the backlog is gone, live resumes
    await stream.aclose()


async def test_subscription_drops_the_backlog_once_and_never_blocks() -> None:
    subscription = Subscription(new_uuid(), 2)
    events = [fake_event(subscription.project_id) for _ in range(5)]

    assert await subscription.get(0.01) is None  # idle: timeout
    for event in events:
        subscription.offer(event)  # the 3rd overflows; the rest are discarded, none blocks

    assert isinstance(await subscription.get(1), ResyncMarker)
    assert await subscription.get(0.01) is None
    subscription.offer(events[0])
    assert await subscription.get(1) == events[0]  # after the marker, delivery resumes


async def test_invalid_last_event_id_is_a_resync_not_an_error(rig: Rig) -> None:
    project = rig.project()
    broker = await rig.broker()
    unknown = EventCursor(1, new_uuid()).encode()  # well-formed, but no such event

    for garbage in ("not-a-cursor", "e1syLCJ4In0", unknown):
        stream = broker.stream(project.project_id, garbage)
        assert (await anext(stream)).kind == "resync"
        assert (await anext(stream)).kind == "heartbeat"  # and the live feed carries on
        await stream.aclose()


async def test_cursor_whose_txid_does_not_match_the_event_is_a_resync(rig: Rig) -> None:
    project = rig.project()
    event = await project.publish("x")
    broker = await rig.broker()

    stream = broker.stream(project.project_id, EventCursor(event.txid + 1, event.id).encode())

    assert (await anext(stream)).kind == "resync"
    await stream.aclose()


async def test_replay_window_exceeded_is_a_resync(rig: Rig) -> None:
    project = rig.project()
    anchor, *missed = await publish_many(project, 4)
    broker = await rig.broker(RealtimeConfig(heartbeat_seconds=0.05, replay_max_events=2))

    stream = broker.stream(project.project_id, cursor_of(anchor))

    assert len(missed) == 3
    assert (await anext(stream)).kind == "resync"
    await stream.aclose()


async def test_replay_exactly_at_the_window_limit_is_served(rig: Rig) -> None:
    project = rig.project()
    anchor, *missed = await publish_many(project, 3)
    broker = await rig.broker(RealtimeConfig(heartbeat_seconds=0.05, replay_max_events=2))

    stream = broker.stream(project.project_id, cursor_of(anchor))

    assert ids(await events_only(stream, 2)) == [e.id for e in missed]
    await stream.aclose()


async def test_cursor_older_than_the_replay_age_is_a_resync(rig: Rig) -> None:
    project = rig.project()
    old = await project.publish("old")  # occurred_at: AT
    broker = await rig.broker()
    rig.clock.set(AT + timedelta(hours=24, seconds=1))

    stream = broker.stream(project.project_id, cursor_of(old))
    assert (await anext(stream)).kind == "resync"
    await stream.aclose()

    rig.clock.set(AT + timedelta(hours=24))  # the boundary is still inside the window
    inside = broker.stream(project.project_id, cursor_of(old))
    assert (await anext(inside)).kind == "heartbeat"
    await inside.aclose()


async def test_disconnect_removes_the_subscription(rig: Rig) -> None:
    project = rig.project()
    broker = await rig.broker(FAST)
    stream = broker.stream(project.project_id)
    await prime(stream)
    assert broker.subscriber_count == 1

    await stream.aclose()

    assert broker.subscriber_count == 0
    broker.unsubscribe(Subscription(project.project_id, 1))  # unknown: ignored


@pytest.mark.parametrize("count", [3])
async def test_pump_pages_through_large_backlogs(rig: Rig, count: int) -> None:
    project = rig.project()
    broker = await rig.broker(RealtimeConfig(heartbeat_seconds=0.05, batch_size=2))
    stream = broker.stream(project.project_id)
    await prime(stream)

    published = await publish_many(project, count)
    await pump_to(broker, *published)

    assert ids(await events_only(stream, count)) == [e.id for e in published]
    await stream.aclose()
