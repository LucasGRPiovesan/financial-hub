"""Cross-project isolation: no principal of project A can see or touch anything of project B.

The routes are not listed by hand: every served route whose path contains ``{project_id}`` is
enumerated through ``effective_routes`` and hit with both principals of A (an owner session and an
API key holding every scope) against B's real resources. A route added later is covered
automatically; one that leaks fails here.

Both answers must be a plain 404 *and* identical to the answer for a project that does not exist, so
the response is not an oracle for which projects exist.
"""

import re
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any

import httpx

from financial_hub.platform.http.auth import EffectiveRoute, effective_routes
from financial_hub.shared.ids import IdPrefix, format_id, new_uuid
from tests.integration.identity.support import Api, Operator, csrf, logged_in
from tests.integration.projects.helpers import (
    ALL_SCOPES,
    ASAAS_CREDENTIALS,
    bearer,
    create_account,
    create_key,
    create_project,
    same_problem,
)

PARAMETER = re.compile(r"\{(\w+)\}")
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
PROBE = "probe"  # a path value for parameters the suite has no real resource for

# Valid bodies for the writes the suite knows. Anything else gets ``{}``: the access check runs
# before body validation, so the probe is still answered by the project lookup.
BODIES: dict[tuple[str, str], dict[str, Any]] = {
    ("POST", "/v1/projects/{project_id}/api-keys"): {"name": "probe", "scopes": ["projects:read"]},
    ("POST", "/v1/projects/{project_id}/provider-accounts"): {
        "provider": "asaas",
        "environment": "sandbox",
        "name": "probe",
        "credentials": {"api_key": "isolation-probe-secret-123456"},
    },
    ("PUT", "/v1/projects/{project_id}/provider-accounts/{account_id}/credentials"): {
        "credentials": {"api_key": "isolation-takeover-secret-123456"}
    },
    ("PATCH", "/v1/projects/{project_id}"): {"name": "Hijacked"},
}

# Routes that must be enumerated: guards against the discovery silently returning nothing.
REQUIRED = {
    "GET /v1/projects/{project_id}",
    "PATCH /v1/projects/{project_id}",
    "GET /v1/projects/{project_id}/api-keys",
    "POST /v1/projects/{project_id}/api-keys",
    "POST /v1/projects/{project_id}/api-keys/{key_id}/revoke",
    "GET /v1/projects/{project_id}/provider-accounts",
    "POST /v1/projects/{project_id}/provider-accounts",
    "GET /v1/projects/{project_id}/provider-accounts/{account_id}",
    "PUT /v1/projects/{project_id}/provider-accounts/{account_id}/credentials",
    "POST /v1/projects/{project_id}/provider-accounts/{account_id}/activate",
    "POST /v1/projects/{project_id}/provider-accounts/{account_id}/deactivate",
}


@dataclass
class Tenant:
    owner: Operator
    project: dict[str, str]
    key: dict[str, Any]
    account: dict[str, Any]

    @property
    def project_id(self) -> str:
        return self.project["id"]


async def tenant(api: Api, name: str) -> Tenant:
    owner = await api.operator()
    project = await create_project(api, owner, name)
    client, csrf_token = await logged_in(api, owner)
    async with aclosing(client):
        key = await create_key(client, csrf_token, project["id"], scopes=ALL_SCOPES)
        account = await create_account(client, csrf_token, project["id"])
    return Tenant(owner=owner, project=project, key=key, account=account)


def project_routes(api: Api) -> list[tuple[str, EffectiveRoute]]:
    """``(method, route)`` for every served route under a project."""
    found = [
        (method, route)
        for route in effective_routes(api.app)
        if "{project_id}" in route.path_format
        for method in sorted(route.methods - {"HEAD", "OPTIONS"})
    ]
    return sorted(found, key=lambda item: (item[1].path_format, item[0]))


def label(method: str, route: EffectiveRoute) -> str:
    return f"{method} {route.path_format}"


def fill(route: EffectiveRoute, **known: str) -> str:
    return PARAMETER.sub(lambda m: known.get(m.group(1), PROBE), route.path_format)


async def probe(
    client: httpx.AsyncClient,
    method: str,
    route: EffectiveRoute,
    headers: dict[str, str],
    **known: str,
) -> httpx.Response:
    body = BODIES.get((method, route.path_format), {}) if method in WRITE_METHODS else None
    return await client.request(
        method, fill(route, **known), headers=headers, json=body if body is not None else None
    )


async def test_the_suite_sees_every_project_route(api: Api) -> None:
    found = {label(method, route) for method, route in project_routes(api)}

    assert found >= REQUIRED
    # every route of every module that is mounted under a project is covered, not only these
    assert len(found) >= len(REQUIRED)


async def test_the_other_projects_are_invisible_to_a_session_and_to_an_api_key(api: Api) -> None:
    a, b = await tenant(api, "Tenant A"), await tenant(api, "Tenant B")
    missing = format_id(IdPrefix.PROJECT, new_uuid())
    session_client, csrf_token = await logged_in(api, a.owner)
    leaks: list[str] = []

    async with aclosing(session_client), api.client() as key_client:
        principals: list[tuple[str, httpx.AsyncClient, dict[str, str]]] = [
            ("session of A", session_client, csrf(csrf_token)),
            ("api key of A", key_client, bearer(a.key["key"])),
        ]
        for method, route in project_routes(api):
            for who, client, headers in principals:
                theirs = await probe(
                    client,
                    method,
                    route,
                    headers,
                    project_id=b.project_id,
                    key_id=b.key["id"],
                    account_id=b.account["id"],
                )
                nothing = await probe(
                    client,
                    method,
                    route,
                    headers,
                    project_id=missing,
                    key_id=b.key["id"],
                    account_id=b.account["id"],
                )
                if theirs.status_code != 404:
                    leaks.append(f"{who}: {label(method, route)} -> {theirs.status_code}")
                elif not same_problem(theirs, nothing):
                    leaks.append(f"{who}: {label(method, route)} differs from a missing project")
                # (the path is echoed as `instance`, so ids put in the path are not a signal)
                for fragment in (b.project["name"], b.project["slug"]):
                    if fragment in theirs.text:
                        leaks.append(f"{who}: {label(method, route)} echoes {fragment}")

    assert leaks == []


async def test_nothing_of_the_other_project_changed(api: Api) -> None:
    a, b = await tenant(api, "Tenant A"), await tenant(api, "Tenant B")
    session_client, csrf_token = await logged_in(api, a.owner)
    async with aclosing(session_client), api.client() as key_client:
        for method, route in project_routes(api):
            for client, headers in (
                (session_client, csrf(csrf_token)),
                (key_client, bearer(a.key["key"])),
            ):
                await probe(
                    client,
                    method,
                    route,
                    headers,
                    project_id=b.project_id,
                    key_id=b.key["id"],
                    account_id=b.account["id"],
                )

    owner_client, _ = await logged_in(api, b.owner)
    async with aclosing(owner_client):
        project = await owner_client.get(f"/v1/projects/{b.project_id}")
        keys = await owner_client.get(f"/v1/projects/{b.project_id}/api-keys")
        accounts = await owner_client.get(f"/v1/projects/{b.project_id}/provider-accounts")
    async with api.client() as client:
        b_key_still_works = await client.get(
            f"/v1/projects/{b.project_id}", headers=bearer(b.key["key"])
        )

    assert project.json() == b.project
    assert [(k["id"], k["status"]) for k in keys.json()["data"]] == [(b.key["id"], "active")]
    assert accounts.json()["data"] == [b.account]
    assert b_key_still_works.status_code == 200


async def test_foreign_ids_under_my_own_project_reach_nothing(api: Api) -> None:
    """A's own project in the path, B's key/account id as the resource: still B's, still hidden."""
    a, b = await tenant(api, "Tenant A"), await tenant(api, "Tenant B")
    session_client, csrf_token = await logged_in(api, a.owner)
    crossing = [
        (method, route)
        for method, route in project_routes(api)
        if "{key_id}" in route.path_format or "{account_id}" in route.path_format
    ]
    unexpected: list[str] = []

    async with aclosing(session_client), api.client() as key_client:
        for method, route in crossing:
            via_session = await probe(
                session_client,
                method,
                route,
                csrf(csrf_token),
                project_id=a.project_id,
                key_id=b.key["id"],
                account_id=b.account["id"],
            )
            via_key = await probe(
                key_client,
                method,
                route,
                bearer(a.key["key"]),
                project_id=a.project_id,
                key_id=b.key["id"],
                account_id=b.account["id"],
            )
            # Another module may type the id differently (a ledger account id is not a provider
            # account id): a 422 for a malformed id reaches nothing either, but 2xx never may.
            own_ids = (
                "/api-keys/" in route.path_format or "/provider-accounts/" in route.path_format
            )
            if via_session.status_code not in ((404,) if own_ids else (404, 422)):
                unexpected.append(f"session: {label(method, route)} -> {via_session.status_code}")
            if via_key.status_code not in ((403, 404) if own_ids else (403, 404, 422)):
                unexpected.append(f"api key: {label(method, route)} -> {via_key.status_code}")
            if ASAAS_CREDENTIALS["api_key"] in via_session.text + via_key.text:
                unexpected.append(f"{label(method, route)} returned a credential")

    owner_client, _ = await logged_in(api, b.owner)
    async with aclosing(owner_client):
        keys = await owner_client.get(f"/v1/projects/{b.project_id}/api-keys")
        accounts = await owner_client.get(f"/v1/projects/{b.project_id}/provider-accounts")

    assert crossing  # the suite really exercised cross-id routes
    assert unexpected == []
    assert [k["status"] for k in keys.json()["data"]] == ["active"]
    assert accounts.json()["data"] == [b.account]


async def test_an_unauthenticated_caller_gets_the_same_401_everywhere(api: Api) -> None:
    b = await tenant(api, "Tenant B")
    missing = format_id(IdPrefix.PROJECT, new_uuid())
    statuses: set[int] = set()

    async with api.client() as client:
        for method, route in project_routes(api):
            theirs = await probe(client, method, route, {}, project_id=b.project_id)
            nothing = await probe(client, method, route, {}, project_id=missing)
            statuses.add(theirs.status_code)
            assert same_problem(theirs, nothing), label(method, route)

    assert statuses == {401}


async def test_the_other_direction_is_isolated_too(api: Api) -> None:
    """B's key and B's session get nothing of A (not only A -> B)."""
    a, b = await tenant(api, "Tenant A"), await tenant(api, "Tenant B")
    async with api.client() as client:
        responses = [
            await client.get(f"/v1/projects/{a.project_id}", headers=bearer(b.key["key"])),
            await client.get(
                f"/v1/projects/{a.project_id}/provider-accounts", headers=bearer(b.key["key"])
            ),
        ]
    session_client, _ = await logged_in(api, b.owner)
    async with aclosing(session_client):
        responses.append(await session_client.get(f"/v1/projects/{a.project_id}/api-keys"))

    assert [r.status_code for r in responses] == [404, 404, 404]
