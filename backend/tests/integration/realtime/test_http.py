"""``GET /v1/projects/{id}/stream``: access control, SSE framing, resume, isolation."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from financial_hub.modules.events.api.stream import encode
from financial_hub.modules.events.application import Event
from financial_hub.modules.events.application.realtime import StreamMessage
from financial_hub.modules.events.domain import EventCursor
from financial_hub.modules.identity.application.api import Role
from financial_hub.shared.ids import IdPrefix, format_id, new_uuid, parse_id
from tests.integration.events.support import Harness
from tests.integration.identity.support import (
    Api,
    Operator,
    csrf,
    logged_in,
    running_api,
    unique_slug,
)
from tests.integration.realtime.support import cookie_header, open_sse


@pytest.fixture
async def api(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Api]:
    """An API whose broker runs the real loop with a fast heartbeat and poll interval."""
    monkeypatch.setenv("FH_EVENTS_STREAM_HEARTBEAT_SECONDS", "0.05")
    monkeypatch.setenv("FH_EVENTS_STREAM_POLL_INTERVAL_SECONDS", "0.05")
    async with running_api() as running:
        yield running


async def create_project(
    api: Api, owner_engine: AsyncEngine, operator: Operator
) -> tuple[str, Harness]:
    """A project owned by ``operator`` plus a harness publishing into it."""
    client, token = await logged_in(api, operator)
    async with aclosing(client):
        created = await client.post(
            "/v1/projects", json={"slug": unique_slug(), "name": "P"}, headers=csrf(token)
        )
    assert created.status_code == 201, created.text
    public_id: str = created.json()["id"]
    harness = Harness(api.factory, owner_engine, parse_id(public_id, IdPrefix.PROJECT))
    return public_id, harness


async def cookie_of(api: Api, operator: Operator) -> str:
    client, _ = await logged_in(api, operator)
    async with aclosing(client):
        return cookie_header(client)


async def subscribed(api: Api, count: int = 1) -> None:
    """Wait until exactly ``count`` connections are registered with the broker."""
    async with asyncio.timeout(5):
        while api.app.state.realtime.subscriber_count != count:  # noqa: ASYNC110
            await asyncio.sleep(0.01)


def path(project_id: str) -> str:
    return f"/v1/projects/{project_id}/stream"


def eid(event: Event) -> str:
    return format_id(IdPrefix.EVENT, event.id)


async def test_anonymous_is_401(api: Api, owner_engine: AsyncEngine) -> None:
    project_id, _ = await create_project(api, owner_engine, await api.operator())

    async with open_sse(api.app, path(project_id)) as response:
        assert response.status == 401


async def test_non_member_gets_404(api: Api, owner_engine: AsyncEngine) -> None:
    project_id, _ = await create_project(api, owner_engine, await api.operator())
    stranger = await cookie_of(api, await api.operator())

    async with open_sse(api.app, path(project_id), cookie=stranger) as response:
        assert response.status == 404
    unknown = format_id(IdPrefix.PROJECT, new_uuid())
    async with open_sse(api.app, path(unknown), cookie=stranger) as response:
        assert response.status == 404


@pytest.mark.parametrize("role", list(Role))
async def test_every_role_may_read_the_stream(
    api: Api, owner_engine: AsyncEngine, role: Role
) -> None:
    project_id, _ = await create_project(api, owner_engine, await api.operator())
    member = await api.operator()
    await api.grant(member, project_id, role)

    async with open_sse(api.app, path(project_id), cookie=await cookie_of(api, member)) as response:
        assert response.status == 200


async def test_live_events_are_framed_as_sse(api: Api, owner_engine: AsyncEngine) -> None:
    owner = await api.operator()
    project_id, project = await create_project(api, owner_engine, owner)
    cause = await project.publish("cause")
    correlation = new_uuid()

    async with open_sse(api.app, path(project_id), cookie=await cookie_of(api, owner)) as response:
        assert response.status == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache"
        assert (await response.frame()).raw == "retry: 3000"
        await subscribed(api)
        event = await project.publish(
            "hi", subject="order-1", causation_id=cause.id, correlation_id=correlation
        )
        # A fresh connection may also get "cause" if the broker had not pumped it yet.
        frame = (await response.event_frames(1))[0]
        if frame.data["subject"] != "order-1":
            frame = (await response.event_frames(1))[0]

    assert EventCursor.decode(frame.field("id") or "") == event.cursor
    assert frame.field("event") == "system.test"
    assert frame.data == {
        "id": eid(event),
        "type": "system.test",
        "version": event.version,
        "project_id": project_id,
        "subject": "order-1",
        "occurred_at": event.occurred_at.isoformat(),
        "correlation_id": str(correlation),
        "causation_id": eid(cause),
        "data": {"message": "hi"},
    }


async def test_envelope_has_nulls_for_absent_references(
    api: Api, owner_engine: AsyncEngine
) -> None:
    owner = await api.operator()
    project_id, project = await create_project(api, owner_engine, owner)

    async with open_sse(api.app, path(project_id), cookie=await cookie_of(api, owner)) as response:
        await subscribed(api)
        await project.publish("plain")
        (frame,) = await response.event_frames(1)

    data = frame.data
    assert (data["subject"], data["correlation_id"], data["causation_id"]) == (None, None, None)


@pytest.mark.parametrize("how", ["header", "query"])
async def test_reconnect_resumes_after_last_event_id(
    api: Api, owner_engine: AsyncEngine, how: str
) -> None:
    owner = await api.operator()
    project_id, project = await create_project(api, owner_engine, owner)
    seen, *missed = [await project.publish(f"m{i}") for i in range(4)]
    cookie = await cookie_of(api, owner)
    last = seen.cursor.encode()

    if how == "header":
        opened = open_sse(api.app, path(project_id), cookie=cookie, headers={"Last-Event-ID": last})
    else:
        opened = open_sse(api.app, path(project_id), cookie=cookie, query=f"last_event_id={last}")
    async with opened as response:
        frames = await response.event_frames(3)
        live = await project.publish("live")
        (after,) = await response.event_frames(1)

    assert [f.data["id"] for f in frames] == [eid(e) for e in missed]
    assert after.data["id"] == eid(live)


async def test_the_header_wins_over_the_query(api: Api, owner_engine: AsyncEngine) -> None:
    owner = await api.operator()
    project_id, project = await create_project(api, owner_engine, owner)
    first, second, third = [await project.publish(f"m{i}") for i in range(3)]

    async with open_sse(
        api.app,
        path(project_id),
        cookie=await cookie_of(api, owner),
        headers={"Last-Event-ID": second.cursor.encode()},
        query=f"last_event_id={first.cursor.encode()}",
    ) as response:
        (frame,) = await response.event_frames(1)

    assert frame.data["id"] == eid(third)


async def test_invalid_last_event_id_is_a_resync_event(api: Api, owner_engine: AsyncEngine) -> None:
    owner = await api.operator()
    project_id, project = await create_project(api, owner_engine, owner)

    async with open_sse(
        api.app,
        path(project_id),
        cookie=await cookie_of(api, owner),
        headers={"Last-Event-ID": "garbage"},
    ) as response:
        assert response.status == 200
        (resync,) = await response.event_frames(1)
        await subscribed(api)
        live = await project.publish("still live")
        (event,) = await response.event_frames(1)

    assert resync.raw == "event: resync\ndata: {}"  # no id: it must not move Last-Event-ID
    assert event.data["id"] == eid(live)


async def test_idle_connections_get_ping_comments(api: Api, owner_engine: AsyncEngine) -> None:
    owner = await api.operator()
    project_id, _ = await create_project(api, owner_engine, owner)

    async with open_sse(api.app, path(project_id), cookie=await cookie_of(api, owner)) as response:
        frames = [await response.frame() for _ in range(3)]

    assert [f.raw for f in frames] == ["retry: 3000", ": ping", ": ping"]


async def test_streams_never_cross_projects(api: Api, owner_engine: AsyncEngine) -> None:
    owner = await api.operator()
    mine_id, mine = await create_project(api, owner_engine, owner)
    _, other = await create_project(api, owner_engine, owner)  # same operator, other project

    async with open_sse(api.app, path(mine_id), cookie=await cookie_of(api, owner)) as response:
        await subscribed(api)
        await other.publish("not here")
        own = await mine.publish("here")
        (frame,) = await response.event_frames(1)
        assert (await response.frame()).raw == ": ping"  # nothing else follows

    assert frame.data["id"] == eid(own)


async def test_disconnect_unsubscribes(api: Api, owner_engine: AsyncEngine) -> None:
    owner = await api.operator()
    project_id, _ = await create_project(api, owner_engine, owner)

    async with open_sse(api.app, path(project_id), cookie=await cookie_of(api, owner)) as response:
        await response.frame()
        await subscribed(api, 1)

    await subscribed(api, 0)


def test_encode_resync_and_heartbeat() -> None:
    assert encode(StreamMessage("heartbeat")) == b": ping\n\n"
    assert encode(StreamMessage("resync")) == b"event: resync\ndata: {}\n\n"
