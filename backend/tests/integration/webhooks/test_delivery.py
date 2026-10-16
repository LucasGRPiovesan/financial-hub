"""Sending to a local allowlisted receiver: delivered, retried, timed out, redirect refused."""

from datetime import timedelta

from financial_hub.modules.webhooks.domain import verify
from tests.integration.identity.support import Api
from tests.integration.webhooks.helpers import hooks


async def test_a_test_event_is_delivered_signed_and_recorded(api: Api) -> None:
    async with hooks(api) as h:
        endpoint = await h.create()
        event_id = await h.ping(endpoint["id"])
        row = await h.drain(event_id)
        attempts = await h.attempts(endpoint["id"])
        explorer = await h.client.get(f"/v1/projects/{h.project_id}/events/{event_id}")

    assert row["status"] == "succeeded" and row["attempts"] == 1
    (request,) = h.target.received
    assert request.path == "/hook"
    assert request.headers["content-type"] == "application/json"
    assert request.headers["webhook-id"] == event_id
    assert request.headers["host"] == f"127.0.0.1:{h.target.port}"
    timestamp = int(request.headers["webhook-timestamp"])
    assert timestamp == int(api.clock.now().timestamp())
    signature = request.headers["webhook-signature"]
    assert signature.startswith("v1,") and " " not in signature
    assert verify(
        endpoint["secret"], signature, message_id=event_id, timestamp=timestamp, body=request.body
    )
    payload = request.json()
    assert payload == explorer.json()["event"]  # the envelope exactly as the explorer shows it
    assert payload["type"] == "webhook.test" and payload["data"]["endpoint_id"] == endpoint["id"]
    (attempt,) = attempts
    assert (attempt["attempt"], attempt["outcome"], attempt["status_code"]) == (1, "delivered", 200)
    assert attempt["error"] is None and attempt["latency_ms"] >= 0
    assert attempt["delivery_id"] == row["id"]


async def test_a_500_schedules_a_retry_that_is_sent_when_due(api: Api) -> None:
    async with hooks(api) as h:
        endpoint = await h.create()
        h.target.status = 500
        event_id = await h.ping(endpoint["id"])
        failed = await h.drain(event_id)
        not_yet = await h.drain(event_id)  # the retry is not due until the clock moves
        h.target.status = 204
        await h.advance(timedelta(seconds=31))
        delivered = await h.drain(event_id)
        attempts = await h.attempts(endpoint["id"])

    assert failed["status"] == "retry_scheduled" and failed["attempts"] == 1
    assert failed["last_error"] == "HTTP 500"
    assert not_yet["status"] == "retry_scheduled"
    assert delivered["status"] == "succeeded" and delivered["attempts"] == 2
    assert len(h.target.received) == 2
    outcomes = sorted((a["attempt"], a["outcome"], a["status_code"]) for a in attempts)
    assert outcomes == [(1, "failed", 500), (2, "delivered", 204)]


async def test_a_slow_receiver_times_out_and_is_retried(api: Api) -> None:
    async with hooks(api) as h:
        h.timeout_seconds = 0.5
        h.target.delay = 3.0
        endpoint = await h.create()
        event_id = await h.ping(endpoint["id"])
        row = await h.drain(event_id)
        (attempt,) = await h.attempts(endpoint["id"])

    assert row["status"] == "retry_scheduled"
    assert attempt["outcome"] == "failed" and attempt["status_code"] is None
    assert "timeout" in attempt["error"] and attempt["latency_ms"] < 3000


async def test_a_redirect_is_not_followed(api: Api) -> None:
    async with hooks(api) as h:
        endpoint = await h.create()
        h.target.status = 302
        h.target.headers = {"Location": "http://169.254.169.254/latest/meta-data"}
        event_id = await h.ping(endpoint["id"])
        row = await h.drain(event_id)
        (attempt,) = await h.attempts(endpoint["id"])

    assert row["status"] == "retry_scheduled"
    assert len(h.target.received) == 1
    assert attempt["status_code"] == 302 and "redirect" in attempt["error"]


async def test_eight_failures_make_the_delivery_dead_and_manual_retry_resends(api: Api) -> None:
    async with hooks(api) as h:
        endpoint = await h.create()
        h.target.status = 503
        event_id = await h.ping(endpoint["id"])
        row = await h.drain(event_id)
        while row["status"] == "retry_scheduled":
            await h.advance(timedelta(hours=30))
            row = await h.drain(event_id)
        h.target.status = 200
        delivery_id = row["id"]
        retry_path = f"/v1/projects/{h.project_id}/events/{event_id}/deliveries/{delivery_id}/retry"
        retried = await h.post(retry_path)
        pending = await h.delivery(event_id)
        redelivered = await h.drain(event_id)

    assert row["status"] == "dead" and row["attempts"] == 8
    assert retried.status_code == 202, retried.text
    assert pending["status"] == "pending"
    assert redelivered["status"] == "succeeded"
    assert len(h.target.received) == 9
