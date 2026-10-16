"""Usage ingestion through HTTP: idempotency, validation, the window, scopes, batches, events."""

from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncEngine

from financial_hub.modules.metering.domain import FUTURE_SKEW, PAST_WINDOW
from tests.integration.identity.support import Api, csrf
from tests.integration.metering.helpers import events_of, meter, scalar, transaction_id
from tests.integration.projects.helpers import bearer, create_key


def pointers(response_body: dict[str, object]) -> list[str]:
    errors = response_body.get("errors") or []
    assert isinstance(errors, list)
    return [error["pointer"] for error in errors]  # pyright: ignore[reportUnknownVariableType]


# -- single event ---------------------------------------------------------------------------------


async def test_ingest_returns_201_and_the_exact_value(api: Api, app_engine: AsyncEngine) -> None:
    async with meter(api) as m:
        await m.metric("gigabytes", "sum", "gb")
        body = m.event("gigabytes", properties={"gb": "12.345678901"})
        created = await m.ingest(body)
        events = await events_of(app_engine, m.project_uuid, body["transaction_id"])

    usage = created.json()
    assert created.status_code == 201, created.text
    assert usage["transaction_id"] == body["transaction_id"]
    assert usage["customer_id"] == m.customer_id and usage["metric"] == "gigabytes"
    assert usage["value"] == body["properties"]["gb"]
    assert usage["properties"] == body["properties"]
    assert events == [
        (
            "usage.recorded",
            {
                "transaction_id": body["transaction_id"],
                "customer_id": m.customer_id,
                "metric": "gigabytes",
                "timestamp": usage["timestamp"],
                "value": usage["value"],
            },
        )
    ]


async def test_customer_by_id_or_ref_and_both_must_agree(api: Api) -> None:
    async with meter(api) as m:
        await m.metric("api_calls")
        by_id = await m.ingest(
            m.event("api_calls", customer_external_ref=None, customer_id=m.customer_id)
        )
        agree = await m.ingest(m.event("api_calls", customer_id=m.customer_id))
        neither = await m.ingest(m.event("api_calls", customer_external_ref=None))
        stranger = await m.ingest(m.event("api_calls", customer_external_ref="nobody"))

    assert by_id.status_code == 201, by_id.text
    assert agree.status_code == 201, agree.text
    assert neither.status_code == 422
    assert stranger.status_code == 422
    assert "/customer_external_ref" in pointers(stranger.json())


async def test_duplicate_with_the_same_payload_returns_the_original_with_200(
    api: Api, app_engine: AsyncEngine
) -> None:
    async with meter(api) as m:
        await m.metric("gigabytes", "sum", "gb")
        body = m.event("gigabytes", properties={"gb": "2.5"})
        first = await m.ingest(body)
        retry = await m.ingest({**body, "properties": {"gb": "2.5"}})
        stamped = await m.ingest({**body, "timestamp": first.json()["timestamp"]})
        count = await scalar(
            app_engine,
            "SELECT count(*) FROM usage_events WHERE project_id = :p",
            p=m.project_uuid,
        )
        events = await events_of(app_engine, m.project_uuid, body["transaction_id"])

    assert first.status_code == 201
    assert retry.status_code == 200, retry.text
    assert retry.json() == first.json()
    assert stamped.status_code == 200 and stamped.json() == first.json()
    assert count == 1
    assert len(events) == 1  # a retry publishes nothing


async def test_duplicate_with_a_different_payload_is_a_409_and_changes_nothing(api: Api) -> None:
    async with meter(api) as m:
        await m.metric("gigabytes", "sum", "gb")
        await m.metric("other", "count")
        body = m.event("gigabytes", properties={"gb": "2.5"})
        first = await m.ingest(body)
        variations = [
            {**body, "properties": {"gb": "2.6"}},
            {**body, "properties": {"gb": "2.5", "extra": 1}},
            {**body, "metric": "other"},
            {**body, "timestamp": (m.api.clock.now() - timedelta(hours=1)).isoformat()},
        ]
        conflicts = [await m.ingest(variation) for variation in variations]
        again = await m.ingest(body)

    assert first.status_code == 201
    for conflict in conflicts:
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["type"].endswith("/usage-transaction-conflict")
    assert again.status_code == 200 and again.json() == first.json()


async def test_a_retry_survives_the_metric_being_archived(api: Api) -> None:
    async with meter(api) as m:
        await m.metric("api_calls")
        body = m.event("api_calls")
        first = await m.ingest(body)
        await m.owner_client.post(f"{m.metrics_path}/api_calls/archive", headers=csrf(m.csrf_token))
        retry = await m.ingest(body)
        fresh = await m.ingest(m.event("api_calls"))

    assert first.status_code == 201
    assert retry.status_code == 200 and retry.json() == first.json()
    assert fresh.status_code == 422
    assert fresh.json()["errors"][0]["code"] == "archived"


async def test_unknown_metric_is_rejected(api: Api) -> None:
    async with meter(api) as m:
        response = await m.ingest(m.event("nope"))

    assert response.status_code == 422
    assert pointers(response.json()) == ["/metric"]
    assert response.json()["errors"][0]["code"] == "not_found"


async def test_properties_must_carry_an_exact_measure(api: Api) -> None:
    async with meter(api) as m:
        await m.metric("gigabytes", "sum", "gb")
        await m.metric("users", "unique_count", "user")
        missing = await m.ingest(m.event("gigabytes"))
        float_value = await m.ingest(m.event("gigabytes", properties={"gb": 1.5}))
        too_precise = await m.ingest(m.event("gigabytes", properties={"gb": "0.0000000001"}))
        no_user = await m.ingest(m.event("users", properties={"user": ""}))
        integer = await m.ingest(m.event("gigabytes", properties={"gb": 7}))

    for rejected in (missing, float_value, too_precise, no_user):
        assert rejected.status_code == 422, rejected.text
        assert pointers(rejected.json()) == ["/properties"]
    assert integer.status_code == 201 and integer.json()["value"] == "7"


async def test_transaction_id_shape(api: Api) -> None:
    async with meter(api) as m:
        await m.metric("api_calls")
        bad = [await m.ingest(m.event("api_calls", transaction_id=value)) for value in ("", "a b")]

    for response in bad:
        assert response.status_code == 422, response.text


async def test_the_accepted_window_is_30_days_back_and_5_minutes_ahead(api: Api) -> None:
    async with meter(api) as m:
        await m.metric("api_calls")
        now = m.api.clock.now()
        edges = {
            "oldest": (now - PAST_WINDOW, 201),
            "too_old": (now - PAST_WINDOW - timedelta(seconds=1), 422),
            "latest": (now + FUTURE_SKEW, 201),
            "too_new": (now + FUTURE_SKEW + timedelta(seconds=1), 422),
        }
        results = {
            name: await m.ingest(m.event("api_calls", timestamp=moment.isoformat()))
            for name, (moment, _) in edges.items()
        }
        naive = await m.ingest(m.event("api_calls", timestamp=now.replace(tzinfo=None).isoformat()))
        defaulted = await m.ingest(m.event("api_calls"))

    for name, (_, expected) in edges.items():
        assert results[name].status_code == expected, (name, results[name].text)
    for name in ("too_old", "too_new"):
        assert pointers(results[name].json()) == ["/timestamp"]
        assert results[name].json()["errors"][0]["code"] == "out_of_window"
    assert naive.status_code == 422
    assert defaulted.status_code == 201
    assert defaulted.json()["timestamp"].startswith(now.strftime("%Y-%m-%dT%H:%M:%S"))


# -- authentication and scope --------------------------------------------------------------------


async def test_a_key_without_usage_write_is_forbidden(api: Api) -> None:
    async with meter(api, scopes=("customers:read",)) as m:
        await m.metric("api_calls")
        single = await m.ingest(m.event("api_calls"))
        batch = await m.batch([m.event("api_calls")])

    assert single.status_code == 403, single.text
    assert batch.status_code == 403, batch.text


async def test_usage_write_alone_reads_nothing(api: Api) -> None:
    async with meter(api) as m:
        usage_key = await create_key(
            m.owner_client, m.csrf_token, m.project_id, scopes=["usage:write"]
        )
        async with api.client() as client:
            customers = await client.get(
                f"/v1/projects/{m.project_id}/customers", headers=bearer(usage_key["key"])
            )
            metrics = await client.get(m.metrics_path, headers=bearer(usage_key["key"]))
            summary = await client.get(
                f"/v1/projects/{m.project_id}/usage/summary", headers=bearer(usage_key["key"])
            )

    assert customers.status_code in (401, 403)
    assert metrics.status_code in (401, 403)
    assert summary.status_code in (401, 403)


async def test_unauthenticated_and_operator_sessions_cannot_ingest(api: Api) -> None:
    async with meter(api) as m:
        await m.metric("api_calls")
        body = m.event("api_calls")
        async with api.client() as client:
            anonymous = await client.post("/v1/usage", json=body)
            bad_key = await client.post("/v1/usage", json=body, headers=bearer("fh_nope"))
        operator = await m.owner_client.post("/v1/usage", json=body, headers=csrf(m.csrf_token))
        operator_batch = await m.owner_client.post(
            "/v1/usage/batch", json={"events": [body]}, headers=csrf(m.csrf_token)
        )

    assert anonymous.status_code == 401
    assert bad_key.status_code == 401
    assert operator.status_code == 403, operator.text
    assert operator_batch.status_code == 403, operator_batch.text


async def test_a_revoked_key_cannot_ingest(api: Api) -> None:
    async with meter(api) as m:
        await m.metric("api_calls")
        usage_key = await create_key(
            m.owner_client, m.csrf_token, m.project_id, scopes=["usage:write"]
        )
        revoke = await m.owner_client.post(
            f"/v1/projects/{m.project_id}/api-keys/{usage_key['id']}/revoke",
            headers=csrf(m.csrf_token),
        )
        response = await m.ingest(m.event("api_calls"), key=usage_key["key"])

    assert revoke.status_code in (200, 204), revoke.text
    assert response.status_code == 401


# -- batches --------------------------------------------------------------------------------------


async def test_batch_is_per_item_a_bad_event_does_not_stop_the_rest(
    api: Api, app_engine: AsyncEngine
) -> None:
    async with meter(api) as m:
        await m.metric("gigabytes", "sum", "gb")
        first = m.event("gigabytes", properties={"gb": "1"})
        await m.ingest(first)
        events = [
            m.event("gigabytes", properties={"gb": "2"}),  # created
            m.event("nope"),  # rejected: unknown metric
            first,  # duplicate of an earlier call
            {**first, "properties": {"gb": "9"}},  # rejected: 409 against the stored event
            m.event("gigabytes", properties={"gb": "3"}),  # created
            m.event("gigabytes", properties={"gb": 1.5}),  # rejected: float
        ]
        response = await m.batch(events)
        retry = await m.batch(events)
        stored = await scalar(
            app_engine,
            "SELECT count(*) FROM usage_events WHERE project_id = :p",
            p=m.project_uuid,
        )

    body = response.json()
    assert response.status_code == 200, response.text
    assert [item["index"] for item in body["results"]] == list(range(6))
    assert [item["status"] for item in body["results"]] == [
        "created",
        "rejected",
        "duplicate",
        "rejected",
        "created",
        "rejected",
    ]
    assert (body["created"], body["duplicates"], body["rejected"]) == (2, 1, 3)
    assert body["results"][1]["error"]["errors"][0]["pointer"] == "/events/1/metric"
    assert body["results"][3]["error"]["status"] == 409
    assert body["results"][3]["usage"] is None
    assert body["results"][0]["usage"]["value"] == "2"
    assert body["results"][2]["usage"]["value"] == "1"
    # Retrying the whole batch: accepted events come back as duplicates, rejected stay rejected.
    assert [item["status"] for item in retry.json()["results"]] == [
        "duplicate",
        "rejected",
        "duplicate",
        "rejected",
        "duplicate",
        "rejected",
    ]
    assert stored == 3  # the one sent first + the two created by the batch


async def test_a_transaction_repeated_inside_a_batch_is_compared_with_its_first_use(
    api: Api,
) -> None:
    async with meter(api) as m:
        await m.metric("gigabytes", "sum", "gb")
        txn = transaction_id()
        response = await m.batch(
            [
                m.event("gigabytes", transaction_id=txn, properties={"gb": "1"}),
                m.event("gigabytes", transaction_id=txn, properties={"gb": "1"}),
                m.event("gigabytes", transaction_id=txn, properties={"gb": "2"}),
            ]
        )

    assert response.status_code == 200, response.text
    assert [item["status"] for item in response.json()["results"]] == [
        "created",
        "duplicate",
        "rejected",
    ]


async def test_batch_size_limits(api: Api, app_engine: AsyncEngine) -> None:
    async with meter(api) as m:
        await m.metric("api_calls")
        empty = await m.batch([])
        full = await m.batch([m.event("api_calls") for _ in range(100)])
        too_many = await m.batch([m.event("api_calls") for _ in range(101)])
        missing_field = await m.batch([{"metric": "api_calls"}])
        count = await scalar(
            app_engine,
            "SELECT count(*) FROM usage_events WHERE project_id = :p",
            p=m.project_uuid,
        )

    assert empty.status_code == 422
    assert full.status_code == 200 and full.json()["created"] == 100
    assert too_many.status_code == 422
    assert missing_field.status_code == 422  # malformed body: the whole request is refused
    assert count == 100  # nothing from the refused batches
