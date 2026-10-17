"""Business events and simulation over HTTP.

``POST /v1/business-events`` (API key with ``events:write``) maps a payload through the active
manifest to usage, exactly once per idempotency key; ``simulate`` answers the same mapping plus
charges and the split allocation without writing a row.
"""

from sqlalchemy.ext.asyncio import AsyncEngine

from tests.integration.identity.support import Api, csrf
from tests.integration.integrations.helpers import EVENT, MANIFEST, PAYLOAD, count, store
from tests.integration.projects.helpers import create_key

TABLES = (
    "usage_events",
    "business_events",
    "integration_manifests",
    "integration_manifest_activations",
    "billable_metrics",
    "webhook_endpoints",
    "events",
)


async def test_an_event_is_recorded_once_and_maps_to_usage(
    api: Api, owner_engine: AsyncEngine
) -> None:
    async with store(api) as s:
        await s.live(MANIFEST)
        body = {"name": EVENT, "payload": PAYLOAD, "idempotency_key": "c-1"}
        first = await s.send(body)
        retry = await s.send(body)
        other = await s.send({**body, "payload": {**PAYLOAD, "minutes": 31}})
        project = s.meter.project_uuid
        usage = await count(owner_engine, "usage_events", project)
        stored = await count(owner_engine, "business_events", project)
        listed = await s.owner.get(f"/v1/projects/{s.meter.project_id}/business-events")

    assert first.status_code == 201, first.text
    assert retry.status_code == 200, retry.text
    assert retry.json() == first.json()
    assert other.status_code == 409, other.text
    assert first.json()["manifest_version"] == 1
    assert (usage, stored) == (2, 1)  # api_calls + consultations, exactly once
    assert [row["idempotency_key"] for row in listed.json()["data"]] == ["c-1"]


async def test_unknown_names_bad_payloads_and_missing_manifests_are_refused(api: Api) -> None:
    async with store(api) as s:
        none_active = await s.send({"name": EVENT, "payload": PAYLOAD, "idempotency_key": "k-1"})
        await s.live(MANIFEST)
        unknown = await s.send({"name": "nope.done", "payload": PAYLOAD, "idempotency_key": "k-2"})
        invalid = await s.send(
            {"name": EVENT, "payload": {**PAYLOAD, "minutes": 0}, "idempotency_key": "k-3"}
        )

    assert none_active.status_code == 409, none_active.text
    assert unknown.status_code == 422, unknown.text
    assert invalid.status_code == 422, invalid.text


async def test_ingestion_needs_the_events_write_scope(api: Api) -> None:
    async with store(api) as s:
        await s.live(MANIFEST)
        usage_only = await create_key(
            s.owner, s.meter.csrf_token, s.meter.project_id, scopes=["usage:write"]
        )
        body = {"name": EVENT, "payload": PAYLOAD, "idempotency_key": "scope-1"}
        denied = await s.send(body, key=usage_only["key"])
        session = await s.owner.post(
            "/v1/business-events", json=body, headers=csrf(s.meter.csrf_token)
        )
        allowed = await s.send(body)

    assert denied.status_code == 403, denied.text
    assert session.status_code == 403, session.text
    assert allowed.status_code == 201, allowed.text


async def test_simulation_rates_and_splits_without_writing_anything(
    api: Api, owner_engine: AsyncEngine
) -> None:
    async with store(api) as s:
        await s.live(MANIFEST)
        project = s.meter.project_uuid
        before = {table: await count(owner_engine, table, project) for table in TABLES}
        client, headers = s.as_owner()
        response = await client.post(
            f"{s.base}/1/simulate", json={"name": EVENT, "payload": PAYLOAD}, headers=headers
        )
        after = {table: await count(owner_engine, table, project) for table in TABLES}

    assert response.status_code == 200, response.text
    assert after == before
    body = response.json()
    assert [draft["metric"] for draft in body["usage"]] == ["api_calls", "consultations"]
    (charge,) = body["charges"]  # only api_calls has a price
    assert charge["amount"] == {"amount_minor": 300, "currency": "BRL"}  # 30 x 0.10
    assert body["allocations"]["BRL"] == [
        {
            "payee": "clinic-1",
            "basis_points": 7000,
            "amount": {"amount_minor": 210, "currency": "BRL"},
        },
        {
            "payee": "platform",
            "basis_points": 3000,
            "amount": {"amount_minor": 90, "currency": "BRL"},
        },
    ]
