"""The usage summary: exact aggregates per metric type over the half-open range [from, to)."""

from datetime import datetime, timedelta
from typing import Any

from tests.integration.identity.support import Api, csrf
from tests.integration.metering.helpers import Meter, meter
from tests.integration.projects.helpers import bearer


async def record(m: Meter, metric: str, when: datetime, **properties: Any) -> None:
    response = await m.ingest(m.event(metric, timestamp=when.isoformat(), properties=properties))
    assert response.status_code == 201, response.text


async def test_sum_is_exact_in_decimals(api: Api) -> None:
    async with meter(api) as m:
        await m.metric("gigabytes", "sum", "gb")
        now = m.api.clock.now()
        for _ in range(10):
            await record(m, "gigabytes", now - timedelta(hours=1), gb="0.1")
        await record(m, "gigabytes", now - timedelta(hours=1), gb="0.000000001")
        await record(m, "gigabytes", now - timedelta(hours=1), gb=3)
        response = await m.summary("gigabytes", now - timedelta(days=1), now)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["value"] == "4.000000001"  # floats would give 4.000000000999...
    assert (body["events"], body["aggregation"], body["metric"]) == (12, "sum", "gigabytes")
    assert body["customer_id"] == m.customer_id


async def test_count_max_and_unique_count(api: Api) -> None:
    async with meter(api) as m:
        await m.metric("api_calls", "count")
        await m.metric("peak", "max", "mb")
        await m.metric("users", "unique_count", "user")
        now = m.api.clock.now()
        when = now - timedelta(minutes=30)
        for _ in range(3):
            await record(m, "api_calls", when)
        for mb in ("10.5", "99.25", "7"):
            await record(m, "peak", when, mb=mb)
        for user in ("ana", "bia", "ana", 7, "7"):
            await record(m, "users", when, user=user)
        window = (now - timedelta(days=1), now)
        calls = await m.summary("api_calls", *window)
        peak = await m.summary("peak", *window)
        users = await m.summary("users", *window)

    assert (calls.json()["value"], calls.json()["events"]) == ("3", 3)
    assert (peak.json()["value"], peak.json()["events"]) == ("99.25", 3)
    assert (users.json()["value"], users.json()["events"]) == ("3", 5)  # ana, bia, 7


async def test_the_range_is_inclusive_of_from_and_exclusive_of_to(api: Api) -> None:
    async with meter(api) as m:
        await m.metric("api_calls", "count")
        now = m.api.clock.now()
        start = now - timedelta(days=2)
        end = now - timedelta(days=1)
        await record(m, "api_calls", start - timedelta(seconds=1))
        await record(m, "api_calls", start)
        await record(m, "api_calls", end - timedelta(seconds=1))
        await record(m, "api_calls", end)
        response = await m.summary("api_calls", start, end)

    assert response.json()["value"] == "2"
    assert response.json()["start"].startswith(start.strftime("%Y-%m-%dT%H:%M:%S"))


async def test_empty_ranges(api: Api) -> None:
    async with meter(api) as m:
        for code, aggregation, field in (
            ("a", "sum", "x"),
            ("b", "count", None),
            ("c", "max", "x"),
            ("d", "unique_count", "x"),
        ):
            await m.metric(code, aggregation, field)
        now = m.api.clock.now()
        results = {
            code: (await m.summary(code, now - timedelta(days=1), now)).json()
            for code in ("a", "b", "c", "d")
        }

    assert [(code, r["value"], r["events"]) for code, r in results.items()] == [
        ("a", "0", 0),
        ("b", "0", 0),
        ("c", None, 0),
        ("d", "0", 0),
    ]


async def test_each_customer_has_their_own_total_and_archived_metrics_stay_readable(
    api: Api,
) -> None:
    async with meter(api) as m:
        await m.metric("gigabytes", "sum", "gb")
        now = m.api.clock.now()
        when = (now - timedelta(hours=2)).isoformat()
        other = await m.ingest(m.event("gigabytes", properties={"gb": "5"}, timestamp=when))
        assert other.status_code == 201
        # a second customer
        async with m.api.client() as client:
            created = await client.put(
                f"/v1/projects/{m.project_id}/customers/buyer-2",
                json={"name": "João", "kind": "individual"},
                headers=bearer(m.key),
            )
        second_id = created.json()["id"]
        await m.ingest(
            m.event(
                "gigabytes",
                customer_external_ref="buyer-2",
                properties={"gb": "7"},
                timestamp=when,
            )
        )
        await m.owner_client.post(f"{m.metrics_path}/gigabytes/archive", headers=csrf(m.csrf_token))
        first = await m.summary("gigabytes", now - timedelta(days=1), now)
        second = await m.summary("gigabytes", now - timedelta(days=1), now, customer_id=second_id)

    assert first.json()["value"] == "5"
    assert second.json()["value"] == "7"


async def test_summary_validation_and_not_found(api: Api) -> None:
    async with meter(api) as m:
        await m.metric("api_calls", "count")
        now = m.api.clock.now()
        reversed_range = await m.summary("api_calls", now, now - timedelta(days=1))
        empty_range = await m.summary("api_calls", now, now)
        unknown_metric = await m.summary("nope", now - timedelta(days=1), now)
        unknown_customer = await m.summary(
            "api_calls", now - timedelta(days=1), now, customer_id="cus_" + "0" * 26
        )
        naive = await m.owner_client.get(
            f"/v1/projects/{m.project_id}/usage/summary",
            params={
                "customer_id": m.customer_id,
                "metric": "api_calls",
                "from": "2026-10-01T00:00:00",
                "to": "2026-10-02T00:00:00",
            },
        )

    assert reversed_range.status_code == 422, reversed_range.text
    assert empty_range.status_code == 422, empty_range.text
    assert unknown_metric.status_code == 404, unknown_metric.text
    assert unknown_customer.status_code in (404, 422), unknown_customer.text
    assert naive.status_code == 422, naive.text
