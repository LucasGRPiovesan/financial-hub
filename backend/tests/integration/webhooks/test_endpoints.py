"""Webhook endpoints through the operator API: secret once, rotation, disable, RBAC, audit."""

from contextlib import aclosing
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from financial_hub.modules.events.application import EventDraft, EventPublisher
from financial_hub.modules.identity.application.api import Role
from financial_hub.modules.webhooks.domain import verify
from financial_hub.platform.db import UnitOfWork
from financial_hub.shared.ids import IdPrefix, parse_id
from tests.integration.identity.support import Api, csrf
from tests.integration.projects.helpers import bearer, create_key, member
from tests.integration.webhooks.helpers import hooks


async def test_the_secret_is_shown_once_and_stored_encrypted(
    api: Api, app_engine: AsyncEngine
) -> None:
    async with hooks(api) as h:
        headers = {**csrf(h.csrf_token), "Idempotency-Key": "create-once"}
        body = {"url": h.target.url, "event_types": ["payment.*"], "description": "Shop"}
        created = await h.client.post(h.path, json=body, headers=headers)
        replay = await h.client.post(h.path, json=body, headers=headers)
        endpoint = created.json()
        fetched = await h.client.get(f"{h.path}/{endpoint['id']}")
        listed = await h.client.get(h.path)
        async with app_engine.connect() as conn:
            stored = (
                await conn.execute(
                    text("SELECT * FROM webhook_endpoints WHERE id = :id"),
                    {"id": parse_id(endpoint["id"], IdPrefix.WEBHOOK_ENDPOINT)},
                )
            ).one()

    assert created.status_code == 201, created.text
    assert created.headers["cache-control"] == "no-store"
    secret = endpoint["secret"]
    assert secret.startswith("whsec_")
    assert endpoint["event_types"] == ["payment.*"] and endpoint["enabled"] is True
    assert replay.status_code == 409 and secret not in replay.text
    assert fetched.status_code == 200 and "secret" not in fetched.json()
    assert secret not in fetched.text and secret not in listed.text
    assert [item["id"] for item in listed.json()["data"]] == [endpoint["id"]]
    assert secret not in repr(stored) and secret.removeprefix("whsec_") not in repr(stored)


async def test_invalid_urls_are_rejected(api: Api) -> None:
    async with hooks(api) as h:
        urls = (
            "http://10.0.0.1/hook",
            "http://169.254.169.254/latest",
            "https://user:pw@example.com/hook",
            "ftp://example.com/hook",
        )
        responses = [
            await h.client.post(
                h.path, json={"url": url, "event_types": ["*"]}, headers=csrf(h.csrf_token)
            )
            for url in urls
        ]

    assert [response.status_code for response in responses] == [422] * len(urls)


async def test_update_changes_the_filter_and_clears_the_description(api: Api) -> None:
    async with hooks(api) as h:
        endpoint = await h.create(description="old")
        updated = await h.client.patch(
            f"{h.path}/{endpoint['id']}",
            json={"event_types": ["payment.succeeded"], "description": None},
            headers=csrf(h.csrf_token),
        )

    assert updated.status_code == 200, updated.text
    assert updated.json()["event_types"] == ["payment.succeeded"]
    assert updated.json()["description"] is None


async def test_rotation_signs_with_both_secrets_for_24_hours(api: Api) -> None:
    async with hooks(api) as h:
        endpoint = await h.create()
        rotated = await h.post(f"{h.path}/{endpoint['id']}/rotate-secret")
        during = await h.ping(endpoint["id"])
        await h.drain(during)
        await h.advance(timedelta(hours=24, minutes=1))
        after = await h.ping(endpoint["id"])
        await h.drain(after)
        fetched = await h.client.get(f"{h.path}/{endpoint['id']}")

    assert rotated.status_code == 200, rotated.text
    assert rotated.headers["cache-control"] == "no-store"
    old, new = endpoint["secret"], rotated.json()["secret"]
    assert new != old and new.startswith("whsec_")
    assert rotated.json()["secret_rotation_ends_at"] is not None
    first, second = h.target.received
    header = first.headers["webhook-signature"]
    ts = int(first.headers["webhook-timestamp"])
    new_signature, old_signature = header.split(" ")
    assert verify(new, new_signature, message_id=during, timestamp=ts, body=first.body)
    assert verify(old, old_signature, message_id=during, timestamp=ts, body=first.body)
    late = second.headers["webhook-signature"]
    late_ts = int(second.headers["webhook-timestamp"])
    assert " " not in late
    assert verify(new, late, message_id=after, timestamp=late_ts, body=second.body)
    assert not verify(old, late, message_id=after, timestamp=late_ts, body=second.body)
    assert fetched.json()["secret_rotation_ends_at"] is None


async def test_a_disabled_endpoint_gets_no_new_deliveries(api: Api) -> None:
    async with hooks(api) as h:
        kept = await h.create()
        dropped = await h.create()
        pending = await h.ping(dropped["id"])  # queued before the disable: held, never sent
        disabled = await h.post(f"{h.path}/{dropped['id']}/disable")
        ping_disabled = await h.post(f"{h.path}/{dropped['id']}/test")
        async with UnitOfWork(api.factory) as uow:
            event = await EventPublisher(clock=api.clock, notifier=None).publish(
                uow,
                EventDraft(
                    type="webhook.test",
                    data={"endpoint_id": kept["id"], "message": "fan-out"},
                    project_id=parse_id(h.project_id, IdPrefix.PROJECT),
                ),
            )
        fanned = await h.deliveries(event.public_id)
        await h.drain(event.public_id)
        held = await h.delivery(pending)

    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["enabled"] is False and disabled.json()["disabled_at"] is not None
    assert ping_disabled.status_code == 409
    assert len(fanned) == 1  # only the enabled endpoint's subscription
    assert held["status"] == "pending"
    assert [r.headers["webhook-id"] for r in h.target.received] == [event.public_id]


async def test_roles_and_api_keys(api: Api) -> None:
    async with hooks(api) as h:
        endpoint = await h.create()
        one = f"{h.path}/{endpoint['id']}"
        results: dict[str, tuple[int, int, int]] = {}
        for role in (Role.ADMIN, Role.FINANCE, Role.VIEWER):
            _, client, token = await member(api, h.project_id, role)
            async with aclosing(client):
                read = await client.get(h.path)
                create = await client.post(
                    h.path,
                    json={"url": h.target.url, "event_types": ["*"]},
                    headers=csrf(token),
                )
                rotate = await client.post(f"{one}/rotate-secret", headers=csrf(token))
            results[role.value] = (read.status_code, create.status_code, rotate.status_code)
        key = await create_key(h.client, h.csrf_token, h.project_id, scopes=["projects:read"])
        async with api.client() as key_client:
            key_read = await key_client.get(h.path, headers=bearer(key["key"]))
        event_id = await h.ping(endpoint["id"])
        row = await h.drain(event_id)
        delivery_id = row["id"]
        _, finance, token = await member(api, h.project_id, Role.FINANCE)
        async with aclosing(finance):
            retry = await finance.post(
                f"/v1/projects/{h.project_id}/events/{event_id}/deliveries/{delivery_id}/retry",
                headers=csrf(token),
            )

    assert results == {
        "admin": (200, 201, 200),
        "finance": (200, 403, 403),
        "viewer": (403, 403, 403),
    }
    assert key_read.status_code == 403
    assert retry.status_code == 403


async def test_audit_records_never_hold_the_secret(api: Api, app_engine: AsyncEngine) -> None:
    async with hooks(api) as h:
        endpoint = await h.create()
        rotated = await h.post(f"{h.path}/{endpoint['id']}/rotate-secret")
        await h.client.patch(
            f"{h.path}/{endpoint['id']}", json={"description": "x"}, headers=csrf(h.csrf_token)
        )
        await h.post(f"{h.path}/{endpoint['id']}/disable")
        async with app_engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT action, target_id, diff::text AS diff FROM audit_records "
                        "WHERE project_id = :p AND target_type = 'webhook_endpoint' "
                        "ORDER BY occurred_at, id"
                    ),
                    {"p": parse_id(h.project_id, IdPrefix.PROJECT)},
                )
            ).all()

    assert {row.action for row in rows} == {
        "webhook_endpoint.created",
        "webhook_endpoint.secret_rotated",
        "webhook_endpoint.updated",
        "webhook_endpoint.disabled",
    }
    assert {row.target_id for row in rows} == {endpoint["id"]}
    dumped = " ".join(str(row.diff) for row in rows)
    for secret in (endpoint["secret"], rotated.json()["secret"]):
        assert secret not in dumped and secret.removeprefix("whsec_") not in dumped


async def test_unknown_ids_are_not_found(api: Api) -> None:
    async with hooks(api) as h:
        endpoint = await h.create()
        event_id = await h.ping(endpoint["id"])
        missing = await h.client.get(f"{h.path}/whe_nope")
        bad_delivery = await h.post(
            f"/v1/projects/{h.project_id}/events/{event_id}/deliveries/dlv_nope/retry"
        )

    assert missing.status_code == 404
    assert bad_delivery.status_code == 404
