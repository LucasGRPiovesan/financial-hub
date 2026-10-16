"""``Idempotency-Key`` on the real creating routes: projects, API keys, provider accounts."""

import asyncio
import secrets
from contextlib import aclosing

import httpx
from sqlalchemy import text

from financial_hub.modules.identity.application.api import Role
from tests.integration.identity.support import Api, csrf, logged_in, unique_slug
from tests.integration.projects.helpers import (
    ASAAS_CREDENTIALS,
    assert_not_leaked,
    create_project,
    problem_type,
)

PROBLEMS = "https://financial-hub.dev/problems/"


def keyed(csrf_token: str, key: str) -> dict[str, str]:
    return {**csrf(csrf_token), "Idempotency-Key": key}


def is_replay(response: httpx.Response) -> bool:
    return response.headers.get("Idempotent-Replayed") == "true"


async def test_concurrent_project_creation_creates_one_project(api: Api) -> None:
    owner = await api.operator()
    client, csrf_token = await logged_in(api, owner)
    async with aclosing(client):
        for _ in range(3):
            slug = unique_slug("idem")
            key = secrets.token_hex(8)
            responses = await asyncio.gather(
                *(
                    client.post(
                        "/v1/projects",
                        json={"slug": slug, "name": "Once"},
                        headers=keyed(csrf_token, key),
                    )
                    for _ in range(10)
                )
            )

            created = [r for r in responses if r.status_code == 201]
            assert created, [r.text for r in responses]
            assert len({r.json()["id"] for r in created}) == 1
            assert sum(not is_replay(r) for r in created) == 1
            for response in responses:
                assert response.status_code in {201, 409}, response.text
                if response.status_code == 409:
                    assert problem_type(response) == f"{PROBLEMS}idempotency-conflict"
            async with api.factory() as session:
                count = await session.scalar(
                    text("SELECT count(*) FROM projects WHERE slug = :slug"), {"slug": slug}
                )
            assert count == 1

            replay = await client.post(
                "/v1/projects", json={"slug": slug, "name": "Once"}, headers=keyed(csrf_token, key)
            )
            assert replay.status_code == 201 and is_replay(replay)
            assert replay.json() == created[0].json()


async def test_an_api_key_secret_is_never_replayed(api: Api) -> None:
    owner = await api.operator()
    first_project = await create_project(api, owner)
    second_project = await create_project(api, owner)
    client, csrf_token = await logged_in(api, owner)
    key = secrets.token_hex(8)
    body = {"name": "ci", "scopes": ["projects:read"]}
    async with aclosing(client):
        created = await client.post(
            f"/v1/projects/{first_project['id']}/api-keys",
            json=body,
            headers=keyed(csrf_token, key),
        )
        retried = await client.post(
            f"/v1/projects/{first_project['id']}/api-keys",
            json=body,
            headers=keyed(csrf_token, key),
        )
        # Same principal and key, another project: its own key space.
        elsewhere = await client.post(
            f"/v1/projects/{second_project['id']}/api-keys",
            json=body,
            headers=keyed(csrf_token, key),
        )

    assert created.status_code == 201
    assert retried.status_code == 409 and is_replay(retried)
    assert problem_type(retried) == f"{PROBLEMS}idempotency-response-unavailable"
    assert_not_leaked(retried.text, created.json()["key"])
    assert elsewhere.status_code == 201 and not is_replay(elsewhere)
    async with api.factory() as session:
        stored = await session.scalar(
            text(
                "SELECT count(*) FROM idempotency_records"
                " WHERE key = :key AND response_body IS NULL"
            ),
            {"key": key},
        )
    assert stored == 2


async def test_provider_account_creation_replays_without_credentials(api: Api) -> None:
    owner = await api.operator()
    project = await create_project(api, owner)
    client, csrf_token = await logged_in(api, owner)
    key = secrets.token_hex(8)
    body = {
        "provider": "asaas",
        "environment": "sandbox",
        "name": "main",
        "credentials": ASAAS_CREDENTIALS,
    }
    path = f"/v1/projects/{project['id']}/provider-accounts"
    async with aclosing(client):
        created = await client.post(path, json=body, headers=keyed(csrf_token, key))
        replay = await client.post(path, json=body, headers=keyed(csrf_token, key))

    assert created.status_code == 201
    assert replay.status_code == 201 and is_replay(replay)
    assert replay.json() == created.json()
    async with api.factory() as session:
        stored = await session.scalar(
            text("SELECT response_body FROM idempotency_records WHERE key = :key"), {"key": key}
        )
    assert_not_leaked(bytes(stored).decode(), *ASAAS_CREDENTIALS.values())


async def test_access_is_checked_before_a_key_is_claimed(api: Api) -> None:
    owner = await api.operator()
    project = await create_project(api, owner)
    outsider = await api.operator()
    client, csrf_token = await logged_in(api, outsider)
    key = secrets.token_hex(8)
    body = {"name": "ci", "scopes": ["projects:read"]}
    async with aclosing(client):
        denied = await client.post(
            f"/v1/projects/{project['id']}/api-keys", json=body, headers=keyed(csrf_token, key)
        )
        malformed = await client.post(
            "/v1/projects/not-a-project/api-keys", json=body, headers=keyed(csrf_token, key)
        )

    assert denied.status_code == 404
    assert malformed.status_code in {400, 404}
    async with api.factory() as session:
        claimed = await session.scalar(
            text("SELECT count(*) FROM idempotency_records WHERE key = :key"), {"key": key}
        )
    assert claimed == 0


async def test_a_viewer_cannot_claim_keys_in_a_project(api: Api) -> None:
    owner = await api.operator()
    project = await create_project(api, owner)
    viewer = await api.operator()
    await api.grant(viewer, project["id"], Role.VIEWER)
    client, csrf_token = await logged_in(api, viewer)
    async with aclosing(client):
        denied = await client.post(
            f"/v1/projects/{project['id']}/api-keys",
            json={"name": "ci", "scopes": ["projects:read"]},
            headers=keyed(csrf_token, secrets.token_hex(8)),
        )
    assert denied.status_code == 403
