"""Tenant isolation of metering: a project's key records only into its own project."""

from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncEngine

from tests.integration.identity.support import Api
from tests.integration.metering.helpers import meter, scalar
from tests.integration.projects.helpers import bearer


async def test_a_key_records_into_its_own_project_only(api: Api, app_engine: AsyncEngine) -> None:
    async with meter(api) as a, meter(api) as b:
        await a.metric("api_calls")
        await b.metric("api_calls")
        recorded = await a.ingest(a.event("api_calls"))
        in_a = await scalar(
            app_engine, "SELECT count(*) FROM usage_events WHERE project_id = :p", p=a.project_uuid
        )
        in_b = await scalar(
            app_engine, "SELECT count(*) FROM usage_events WHERE project_id = :p", p=b.project_uuid
        )

    assert recorded.status_code == 201
    assert (in_a, in_b) == (1, 0)


async def test_project_a_key_cannot_use_project_b_customers_or_metrics(api: Api) -> None:
    async with meter(api) as a, meter(api) as b:
        await a.metric("only_a")
        await b.metric("only_b")
        # B's customer is not resolvable through A's key, by external ref or by id
        other_ref = a.event("only_a", customer_external_ref="buyer-1")
        await a.ingest(other_ref)  # A's own buyer-1 exists: sanity, creates it
        by_foreign_id = await a.ingest(
            a.event("only_a", customer_external_ref=None, customer_id=b.customer_id)
        )
        foreign_metric = await a.ingest(a.event("only_b"))

    assert by_foreign_id.status_code == 422, by_foreign_id.text
    assert by_foreign_id.json()["errors"][0]["pointer"] == "/customer_id"
    assert foreign_metric.status_code == 422, foreign_metric.text
    assert foreign_metric.json()["errors"][0]["pointer"] == "/metric"


async def test_transaction_ids_are_scoped_to_the_project(api: Api, app_engine: AsyncEngine) -> None:
    async with meter(api) as a, meter(api) as b:
        await a.metric("api_calls")
        await b.metric("api_calls")
        shared = "evt-shared-1"
        first = await a.ingest(a.event("api_calls", transaction_id=shared))
        second = await b.ingest(b.event("api_calls", transaction_id=shared))
        conflict_across = await b.ingest(
            b.event("api_calls", transaction_id=shared, properties={"x": 1})
        )

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text  # same id, other project: independent
    assert conflict_across.status_code == 409  # inside B it is B's event that is compared
    assert first.json()["customer_id"] == a.customer_id
    assert second.json()["customer_id"] == b.customer_id


async def test_operators_cannot_read_or_manage_another_projects_metering(api: Api) -> None:
    async with meter(api) as a, meter(api) as b:
        await b.metric("api_calls")
        now = a.api.clock.now()
        metrics = await a.owner_client.get(b.metrics_path)
        one = await a.owner_client.get(f"{b.metrics_path}/api_calls")
        summary = await a.owner_client.get(
            f"/v1/projects/{b.project_id}/usage/summary",
            params={
                "customer_id": b.customer_id,
                "metric": "api_calls",
                "from": (now - timedelta(days=1)).isoformat(),
                "to": now.isoformat(),
            },
        )

    for response in (metrics, one, summary):
        assert response.status_code in (403, 404), response.text


async def test_a_key_of_project_a_cannot_reach_project_b_routes(api: Api) -> None:
    async with meter(api) as a, meter(api) as b, api.client() as client:
        response = await client.get(b.metrics_path, headers=bearer(a.key))

    assert response.status_code in (401, 403, 404)
