"""Builders shared by the projects, providers and isolation suites (real Postgres, ``fh_app``)."""

import json
from collections.abc import Iterable
from contextlib import aclosing
from typing import Any

import httpx

from financial_hub.modules.identity.application.api import Role
from tests.integration.identity.support import (
    Api,
    Operator,
    csrf,
    logged_in,
    unique_slug,
)

ALL_SCOPES = [
    "projects:read",
    "customers:read",
    "customers:write",
    "ledger:read",
    "payments:read",
    "payments:write",
    "billing:read",
    "billing:write",
    "events:read",
]
ASAAS_SECRET = "asaas_live_9f8e7d6c5b4a3210WXYZ"
ASAAS_CREDENTIALS = {"api_key": ASAAS_SECRET, "webhook_token": "whk_0123456789abcdef"}


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def problem_type(response: httpx.Response) -> str:
    return response.json()["type"]


def same_problem(first: httpx.Response, second: httpx.Response) -> bool:
    """Same status and same problem, ignoring ``instance``-like per-request fields."""
    keys = ("type", "title", "status", "detail")
    a, b = first.json(), second.json()
    return first.status_code == second.status_code and all(a.get(k) == b.get(k) for k in keys)


async def create_project(api: Api, owner: Operator, name: str = "Project") -> dict[str, str]:
    client, csrf_token = await logged_in(api, owner)
    async with aclosing(client):
        response = await client.post(
            "/v1/projects", json={"slug": unique_slug(), "name": name}, headers=csrf(csrf_token)
        )
    assert response.status_code == 201, response.text
    return response.json()


async def member(api: Api, project_id: str, role: Role) -> tuple[Operator, httpx.AsyncClient, str]:
    """A new operator with ``role`` in the project, logged in (close the client with aclosing)."""
    operator = await api.operator()
    await api.grant(operator, project_id, role)
    client, csrf_token = await logged_in(api, operator)
    return operator, client, csrf_token


async def create_key(
    client: httpx.AsyncClient,
    csrf_token: str,
    project_id: str,
    *,
    scopes: Iterable[str] = ("projects:read",),
    name: str = "integration",
    **extra: Any,
) -> dict[str, Any]:
    response = await client.post(
        f"/v1/projects/{project_id}/api-keys",
        json={"name": name, "scopes": list(scopes), **extra},
        headers=csrf(csrf_token),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def bearer_get(api: Api, key: str, path: str) -> httpx.Response:
    async with api.client() as client:
        return await client.get(path, headers=bearer(key))


async def create_account(
    client: httpx.AsyncClient,
    csrf_token: str,
    project_id: str,
    *,
    name: str = "main",
    environment: str = "sandbox",
    credentials: dict[str, str] | None = None,
) -> dict[str, Any]:
    response = await client.post(
        f"/v1/projects/{project_id}/provider-accounts",
        json={
            "provider": "asaas",
            "environment": environment,
            "name": name,
            "credentials": credentials or ASAAS_CREDENTIALS,
        },
        headers=csrf(csrf_token),
    )
    assert response.status_code == 201, response.text
    return response.json()


def assert_not_leaked(haystack: str, *secrets: str) -> None:
    for secret in secrets:
        assert secret not in haystack, "a secret leaked"


def dumps(value: object) -> str:
    return json.dumps(value, default=str)
