"""Event Explorer API: filters, pagination, causality, correlations, deliveries, isolation."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from financial_hub.modules.events.application import Event, explorer
from financial_hub.modules.events.domain import EventCursor
from financial_hub.modules.events.registry import SYSTEM_TEST_HANDLED
from financial_hub.modules.identity.application.api import Role
from financial_hub.platform.db import create_session_factory
from financial_hub.shared.ids import IdPrefix, format_id, new_uuid, parse_id
from tests.integration.events.support import AT, Harness
from tests.integration.identity.support import Api, Operator, logged_in
from tests.integration.ledger.support import CASH, REVENUE, Ledger, make_app_engine, transfer
from tests.integration.projects.helpers import bearer, create_key, create_project, member

HANDLED = SYSTEM_TEST_HANDLED.name


@dataclass
class Rig:
    api: Api
    owner: Operator
    project_id: str
    harness: Harness
    client: httpx.AsyncClient

    @property
    def base(self) -> str:
        return f"/v1/projects/{self.project_id}"

    async def get(self, suffix: str, **params: Any) -> httpx.Response:
        return await self.client.get(f"{self.base}{suffix}", params=params)

    async def ids(self, **params: Any) -> list[str]:
        response = await self.get("/events", **params)
        assert response.status_code == 200, response.text
        return [item["id"] for item in response.json()["data"]]

    async def settled(self, count: int) -> list[str]:
        """The listing once the visibility horizon has passed ``count`` events."""
        async with asyncio.timeout(10):
            while len(found := await self.ids(limit=200)) < count:  # noqa: ASYNC110
                await asyncio.sleep(0.02)
        return found


def eid(event: Event) -> str:
    return format_id(IdPrefix.EVENT, event.id)


@pytest.fixture
async def rig(api: Api, owner_engine: AsyncEngine) -> AsyncIterator[Rig]:
    owner = await api.operator()
    project = await create_project(api, owner)
    harness = Harness(api.factory, owner_engine, parse_id(project["id"], IdPrefix.PROJECT))
    harness.clock.set(api.clock.now())
    client, _ = await logged_in(api, owner)
    async with aclosing(client):
        yield Rig(api, owner, project["id"], harness, client)


async def test_list_is_newest_first_and_filters_by_type(rig: Rig) -> None:
    first = await rig.harness.publish("a")
    second = await rig.harness.publish("b", type=HANDLED)
    third = await rig.harness.publish("c")

    assert await rig.settled(3) == [eid(third), eid(second), eid(first)]
    assert await rig.ids(type="system.test") == [eid(third), eid(first)]
    assert await rig.ids(type=HANDLED) == [eid(second)]
    assert await rig.ids(type="system.*") == [eid(third), eid(second), eid(first)]
    assert await rig.ids(type="*") == [eid(third), eid(second), eid(first)]
    assert await rig.ids(type="billing.*") == []


async def test_list_filters_by_correlation_subject_and_period(rig: Rig) -> None:
    corr = new_uuid()
    old = await rig.harness.publish("old", occurred_at=AT, subject="s1")
    mid = await rig.harness.publish("mid", occurred_at=AT + timedelta(days=1), correlation_id=corr)
    new = await rig.harness.publish("new", occurred_at=AT + timedelta(days=2), subject="s1")
    await rig.settled(3)

    assert await rig.ids(correlation_id=str(corr)) == [eid(mid)]
    assert await rig.ids(subject="s1") == [eid(new), eid(old)]
    assert await rig.ids(occurred_from=(AT + timedelta(days=1)).isoformat()) == [
        eid(new),
        eid(mid),
    ]
    assert await rig.ids(occurred_to=(AT + timedelta(days=1)).isoformat()) == [eid(mid), eid(old)]
    window = {
        "occurred_from": (AT + timedelta(hours=1)).isoformat(),
        "occurred_to": (AT + timedelta(days=1, hours=1)).isoformat(),
    }
    assert await rig.ids(**window) == [eid(mid)]


async def test_cursor_pagination_is_stable_and_complete(rig: Rig) -> None:
    published = [await rig.harness.publish(f"e{i}") for i in range(7)]
    expected = [eid(e) for e in reversed(published)]
    await rig.settled(7)

    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, Any] = {"limit": 3}
        if cursor:
            params["cursor"] = cursor
        body = (await rig.get("/events", **params)).json()
        seen += [item["id"] for item in body["data"]]
        pages += 1
        if not body["has_more"]:
            assert body["next_cursor"] is None
            break
        assert len(body["data"]) == 3
        cursor = body["next_cursor"]
        assert cursor
        # The cursor is the position of the last event of the page.
        assert EventCursor.decode(cursor).id == parse_id(body["data"][-1]["id"], IdPrefix.EVENT)
    assert (seen, pages) == (expected, 3)

    # Events published meanwhile never disturb an in-flight cursor (they sort above it).
    first = (await rig.get("/events", limit=2)).json()
    await rig.harness.publish("late")
    rest = (await rig.get("/events", limit=200, cursor=first["next_cursor"])).json()
    assert [i["id"] for i in first["data"]] + [i["id"] for i in rest["data"]] == expected


async def test_list_validation_errors(rig: Rig) -> None:
    cases: list[dict[str, Any]] = [
        {"type": "Not A Type"},
        {"correlation_id": "nope"},
        {"limit": 0},
        {"limit": 201},
        {"cursor": "!!!"},
        {"occurred_from": "2026-01-01T00:00:00"},
        {"occurred_to": "2026-01-01T00:00:00"},
    ]
    for params in cases:
        response = await rig.get("/events", **params)
        assert response.status_code == 422, (params, response.text)


async def test_event_detail_renders_typeids_and_deliveries(rig: Rig) -> None:
    handler, subscription_id = await rig.harness.subscribe()
    cause = await rig.harness.publish("cause")
    corr = new_uuid()
    event = await rig.harness.publish(
        "x", subject="sub", causation_id=cause.id, correlation_id=corr
    )

    response = await rig.get(f"/events/{eid(event)}")

    assert response.status_code == 200, response.text
    body = response.json()
    shown = body["event"]
    assert shown["id"] == eid(event)
    assert shown["project_id"] == rig.project_id
    assert shown["causation_id"] == eid(cause)
    assert shown["correlation_id"] == str(corr)
    assert (shown["type"], shown["subject"], shown["data"]) == (
        "system.test",
        "sub",
        {"message": "x"},
    )
    assert shown["txid"] == event.txid
    [delivery] = body["deliveries"]
    assert delivery["id"].startswith("dlv_")
    assert delivery["subscription_id"] == str(subscription_id)
    assert (delivery["kind"], delivery["handler"], delivery["status"], delivery["attempts"]) == (
        "internal",
        handler,
        "pending",
        0,
    )
    assert delivery["last_error"] is None


async def test_deliveries_per_subscription_with_truncated_error(rig: Rig) -> None:
    first, first_id = await rig.harness.subscribe()
    second, second_id = await rig.harness.subscribe()
    event = await rig.harness.publish("x")
    await rig.harness.publish("other")  # its deliveries must not show up
    async with rig.harness.owner_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE event_deliveries SET status = 'retry_scheduled', attempts = 3, "
                "last_error = :e "
                "WHERE event_id = :id AND subscription_id = :s"
            ),
            {"e": "x" * 2000, "id": event.id, "s": first_id},
        )

    response = await rig.get(f"/events/{eid(event)}/deliveries")

    assert response.status_code == 200, response.text
    rows = {row["handler"]: row for row in response.json()}
    assert set(rows) == {first, second}
    assert (rows[first]["status"], rows[first]["attempts"]) == ("retry_scheduled", 3)
    assert rows[first]["subscription_id"] == str(first_id)
    assert len(rows[first]["last_error"]) == explorer.LAST_ERROR_LIMIT
    assert rows[first]["last_error"].endswith("...")
    assert (rows[second]["status"], rows[second]["attempts"]) == ("pending", 0)
    assert rows[second]["subscription_id"] == str(second_id)


async def test_deliveries_empty_without_subscriptions(rig: Rig) -> None:
    event = await rig.harness.publish("x")

    response = await rig.get(f"/events/{eid(event)}/deliveries")

    assert (response.status_code, response.json()) == (200, [])
    detail = await rig.get(f"/events/{eid(event)}")
    assert detail.json()["deliveries"] == []


async def chain(rig: Rig, length: int) -> list[Event]:
    events = [await rig.harness.publish("e0")]
    for i in range(1, length):
        events.append(await rig.harness.publish(f"e{i}", causation_id=events[-1].id))
    return events


async def test_causality_three_level_tree_from_any_node(rig: Rig) -> None:
    root = await rig.harness.publish("root")
    mid = await rig.harness.publish("mid", causation_id=root.id)
    sibling = await rig.harness.publish("sibling", causation_id=root.id)
    leaf = await rig.harness.publish("leaf", causation_id=mid.id)

    from_root = (await rig.get(f"/events/{eid(root)}/causality")).json()
    assert from_root["root"]["id"] == eid(root)
    assert from_root["ancestors"] == []
    [first, second] = from_root["descendants"]
    assert first["event"]["id"] == eid(mid)
    assert [c["event"]["id"] for c in first["children"]] == [eid(leaf)]
    assert first["children"][0]["children"] == []
    assert second["event"]["id"] == eid(sibling)
    assert (from_root["node_count"], from_root["truncated"]) == (4, False)

    from_mid = (await rig.get(f"/events/{eid(mid)}/causality")).json()
    assert [a["id"] for a in from_mid["ancestors"]] == [eid(root)]
    assert [d["event"]["id"] for d in from_mid["descendants"]] == [eid(leaf)]
    assert from_mid["node_count"] == 3  # the sibling is not a descendant of mid

    from_leaf = (await rig.get(f"/events/{eid(leaf)}/causality")).json()
    assert [a["id"] for a in from_leaf["ancestors"]] == [eid(mid), eid(root)]
    assert from_leaf["descendants"] == []


async def test_causality_depth_and_node_bounds(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    events = await chain(rig, 6)
    monkeypatch.setattr(explorer, "MAX_CAUSALITY_DEPTH", 2)

    down = (await rig.get(f"/events/{eid(events[0])}/causality")).json()
    level2 = down["descendants"][0]["children"][0]
    assert level2["children"] == []
    assert (down["node_count"], down["truncated"]) == (3, True)

    up = (await rig.get(f"/events/{eid(events[-1])}/causality")).json()
    assert [a["id"] for a in up["ancestors"]] == [eid(events[4]), eid(events[3])]
    assert up["truncated"] is True

    monkeypatch.setattr(explorer, "MAX_CAUSALITY_DEPTH", 20)
    monkeypatch.setattr(explorer, "MAX_CAUSALITY_NODES", 3)
    capped = (await rig.get(f"/events/{eid(events[0])}/causality")).json()
    assert (capped["node_count"], capped["truncated"]) == (3, True)


async def test_causality_ignores_cycles_and_missing_parents(rig: Rig) -> None:
    a = await rig.harness.publish("a")
    b = await rig.harness.publish("b", causation_id=a.id)
    ghost = await rig.harness.publish("ghost", causation_id=new_uuid())
    async with rig.harness.owner_engine.begin() as conn:
        await conn.execute(text("ALTER TABLE events DISABLE TRIGGER USER"))
        try:
            await conn.execute(
                text("UPDATE events SET causation_id = :b WHERE id = :a"), {"a": a.id, "b": b.id}
            )
        finally:
            await conn.execute(text("ALTER TABLE events ENABLE TRIGGER USER"))

    cyclic = (await rig.get(f"/events/{eid(a)}/causality")).json()
    assert [x["id"] for x in cyclic["ancestors"]] == [eid(b)]
    assert [d["event"]["id"] for d in cyclic["descendants"]] == [eid(b)]
    assert cyclic["descendants"][0]["children"] == []

    orphan = (await rig.get(f"/events/{eid(ghost)}/causality")).json()
    assert orphan["ancestors"] == []


async def test_correlation_chain_and_linked_journal_entries(rig: Rig) -> None:
    corr = new_uuid()
    first = await rig.harness.publish("one", correlation_id=corr)
    second = await rig.harness.publish("two", correlation_id=corr, causation_id=first.id)
    await rig.harness.publish("unrelated")
    project = rig.harness.project_id
    engine = make_app_engine()
    try:
        ledger = Ledger(create_session_factory(engine))
        by_cause = replace(transfer(project, "k1", CASH, REVENUE, 100), causation_id=second.id)
        by_corr = replace(transfer(project, "k2", CASH, REVENUE, 50), correlation_id=corr)
        unlinked = transfer(project, "k3", CASH, REVENUE, 7)
        entries = await ledger.post(by_cause, by_corr, unlinked)
    finally:
        await engine.dispose()
    linked = {format_id(IdPrefix.JOURNAL_ENTRY, e.id) for e in entries[:2]}

    response = await rig.get(f"/correlations/{corr}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["correlation_id"] == str(corr)
    assert [e["id"] for e in body["events"]] == [eid(first), eid(second)]
    assert {j["id"] for j in body["journal_entries"]} == linked
    assert {j["recipe"] for j in body["journal_entries"]} == {"test"}

    tree = (await rig.get(f"/events/{eid(first)}/causality")).json()
    assert {j["id"] for j in tree["journal_entries"]} == linked
    entry = next(j for j in tree["journal_entries"] if j["causation_id"] is not None)
    assert entry["causation_id"] == eid(second)
    assert entry["reverses_entry_id"] is None


async def test_unknown_correlation_is_an_empty_chain_and_malformed_is_404(rig: Rig) -> None:
    empty = await rig.get(f"/correlations/{new_uuid()}")
    assert empty.status_code == 200
    assert (empty.json()["events"], empty.json()["journal_entries"]) == ([], [])
    assert (await rig.get("/correlations/not-a-uuid")).status_code == 404


async def test_other_projects_event_is_404_on_every_route(
    rig: Rig, api: Api, owner_engine: AsyncEngine
) -> None:
    other = await create_project(api, rig.owner, "Other")
    foreign = Harness(api.factory, owner_engine, parse_id(other["id"], IdPrefix.PROJECT))
    foreign.clock.set(api.clock.now())
    corr = new_uuid()
    event = await foreign.publish("secret", correlation_id=corr)
    await foreign.subscribe()

    for suffix in ("", "/causality", "/deliveries"):
        response = await rig.get(f"/events/{eid(event)}{suffix}")
        assert response.status_code == 404, suffix
    assert (await rig.get(f"/events/{format_id(IdPrefix.EVENT, new_uuid())}")).status_code == 404
    assert (await rig.get("/events/not-a-typeid")).status_code == 404
    assert (await rig.get(f"/correlations/{corr}")).json()["events"] == []
    assert await rig.ids() == []  # the listing never leaks it either


async def test_anonymous_and_non_member(rig: Rig, api: Api) -> None:
    event = await rig.harness.publish("x")
    paths = [
        "/events",
        f"/events/{eid(event)}",
        f"/events/{eid(event)}/causality",
        f"/events/{eid(event)}/deliveries",
        f"/correlations/{new_uuid()}",
    ]
    stranger, _ = await logged_in(api, await api.operator())
    async with aclosing(stranger), api.client() as anonymous:
        for suffix in paths:
            assert (await anonymous.get(f"{rig.base}{suffix}")).status_code == 401, suffix
            assert (await stranger.get(f"{rig.base}{suffix}")).status_code == 404, suffix


@pytest.mark.parametrize("role", list(Role))
async def test_every_role_may_read(rig: Rig, role: Role) -> None:
    _, client, _ = await member(rig.api, rig.project_id, role)
    async with aclosing(client):
        assert (await client.get(f"{rig.base}/events")).status_code == 200


async def test_api_key_needs_the_events_read_scope(rig: Rig) -> None:
    event = await rig.harness.publish("x")
    client, token = await logged_in(rig.api, rig.owner)
    async with aclosing(client):
        denied = await create_key(client, token, rig.project_id, scopes=["projects:read"])
        allowed = await create_key(client, token, rig.project_id, scopes=["events:read"])

    async with rig.api.client() as bare:
        for suffix in ("/events", f"/events/{eid(event)}", f"/events/{eid(event)}/deliveries"):
            url = f"{rig.base}{suffix}"
            assert (await bare.get(url, headers=bearer(denied["key"]))).status_code == 403, suffix
            assert (await bare.get(url, headers=bearer(allowed["key"]))).status_code == 200, suffix
