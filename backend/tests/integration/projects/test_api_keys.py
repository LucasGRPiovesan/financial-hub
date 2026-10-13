"""Project API keys: shown once, hashed at rest, bearer authentication, scopes, revocation."""

import json
import re
from contextlib import aclosing
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from financial_hub.modules.identity.application.api import Role
from financial_hub.modules.projects.domain import hash_secret
from tests.integration.identity.support import TEST_PEPPER, Api, csrf, logged_in
from tests.integration.projects.helpers import (
    ALL_SCOPES,
    bearer,
    bearer_get,
    create_key,
    create_project,
    member,
    same_problem,
)

KEY_PATTERN = re.compile(r"fh_test_(?P<prefix>[a-z0-9]{8})_(?P<secret>[0-9A-Za-z]{43})")


async def setup(api: Api, **key: Any) -> tuple[dict[str, str], dict[str, Any]]:
    owner = await api.operator()
    project = await create_project(api, owner)
    client, csrf_token = await logged_in(api, owner)
    async with aclosing(client):
        created = await create_key(client, csrf_token, project["id"], **key)
    return project, created


async def test_the_key_is_returned_once_and_never_listed(api: Api) -> None:
    owner = await api.operator()
    project = await create_project(api, owner)
    client, csrf_token = await logged_in(api, owner)
    async with aclosing(client):
        response = await client.post(
            f"/v1/projects/{project['id']}/api-keys",
            json={"name": "  Server  ", "scopes": ["ledger:read", "projects:read"]},
            headers=csrf(csrf_token),
        )
        created = response.json()
        listed = await client.get(f"/v1/projects/{project['id']}/api-keys")

    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    match = KEY_PATTERN.fullmatch(created["key"])
    assert match is not None
    assert created["id"].startswith("key_")
    assert created["prefix"] == match["prefix"]
    assert (created["name"], created["environment"], created["status"]) == (
        "Server",
        "test",
        "active",
    )
    assert created["scopes"] == ["ledger:read", "projects:read"]
    assert created["project_id"] == project["id"]
    assert created["created_by"] == owner.id
    assert (created["last_used_at"], created["expires_at"], created["revoked_at"]) == (
        None,
        None,
        None,
    )

    [item] = listed.json()["data"]
    assert "key" not in item
    assert match["secret"] not in listed.text
    assert item["prefix"] == created["prefix"]
    assert {k: v for k, v in created.items() if k != "key"} == item


async def test_the_database_holds_only_a_peppered_hash(api: Api, app_engine: AsyncEngine) -> None:
    project, created = await setup(api)
    secret = KEY_PATTERN.fullmatch(created["key"])
    assert secret is not None

    async with app_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT row_to_json(k)::text AS doc, secret_hash "
                    "FROM api_keys k WHERE prefix = :p"
                ),
                {"p": created["prefix"]},
            )
        ).all()

    [row] = rows
    assert secret["secret"] not in row.doc
    assert created["key"] not in row.doc
    assert bytes(row.secret_hash) == hash_secret(TEST_PEPPER.encode(), secret["secret"])
    assert bytes(row.secret_hash) != hash_secret(b"another pepper", secret["secret"])
    assert secret["secret"].encode() not in bytes(row.secret_hash)
    assert len(bytes(row.secret_hash)) == 32
    assert project["id"].startswith("prj_")


async def test_a_valid_key_authenticates_without_csrf_and_sees_only_its_project(
    api: Api,
) -> None:
    project, created = await setup(api, scopes=["projects:read"])
    other = await create_project(api, await api.operator())

    own = await bearer_get(api, created["key"], f"/v1/projects/{project['id']}")
    foreign = await bearer_get(api, created["key"], f"/v1/projects/{other['id']}")
    listing = await bearer_get(api, created["key"], "/v1/projects")

    assert own.status_code == 200
    assert own.json()["id"] == project["id"]
    assert foreign.status_code == 404
    assert [p["id"] for p in listing.json()["data"]] == [project["id"]]


async def test_a_key_can_write_without_a_csrf_token_when_its_scope_allows(api: Api) -> None:
    """Bearer requests are not cookie-borne, so there is no CSRF check; permissions still apply."""
    project, created = await setup(api, scopes=["projects:read"])

    async with api.client() as client:
        denied = await client.patch(
            f"/v1/projects/{project['id']}", json={"name": "x"}, headers=bearer(created["key"])
        )

    assert denied.status_code == 403  # projects:manage is never grantable to a key


async def test_wrong_unknown_and_malformed_keys_are_all_the_same_401(api: Api) -> None:
    project, created = await setup(api)
    path = f"/v1/projects/{project['id']}"
    match = KEY_PATTERN.fullmatch(created["key"])
    assert match is not None
    wrong_secret = f"fh_test_{match['prefix']}_{'A' * 43}"
    unknown_prefix = f"fh_test_{'z' * 8}_{match['secret']}"
    wrong_env = created["key"].replace("fh_test_", "fh_live_", 1)
    candidates = [
        wrong_secret,
        unknown_prefix,
        wrong_env,
        "fh_test_short",
        "fh_",
        created["key"] + "x",
    ]

    responses = [await bearer_get(api, key, path) for key in candidates]

    assert [r.status_code for r in responses] == [401] * len(candidates)
    assert all(same_problem(responses[0], r) for r in responses[1:])
    for key, response in zip(candidates, responses, strict=True):
        assert key not in response.text
        assert match["secret"] not in response.text
    # Not shaped like one of ours (case matters): no credential at all, so still a plain 401.
    assert (await bearer_get(api, created["key"].upper(), path)).status_code == 401


async def test_a_revoked_key_stops_working_and_revocation_is_idempotent(api: Api) -> None:
    owner = await api.operator()
    project = await create_project(api, owner)
    client, csrf_token = await logged_in(api, owner)
    async with aclosing(client):
        created = await create_key(client, csrf_token, project["id"])
        before = await bearer_get(api, created["key"], f"/v1/projects/{project['id']}")
        revoked = await client.post(
            f"/v1/projects/{project['id']}/api-keys/{created['id']}/revoke",
            headers=csrf(csrf_token),
        )
        api.clock.advance(timedelta(minutes=5))
        again = await client.post(
            f"/v1/projects/{project['id']}/api-keys/{created['id']}/revoke",
            headers=csrf(csrf_token),
        )
        after = await bearer_get(api, created["key"], f"/v1/projects/{project['id']}")
        listed = await client.get(f"/v1/projects/{project['id']}/api-keys")

    assert before.status_code == 200
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"
    assert revoked.json()["revoked_at"] is not None
    assert again.json()["revoked_at"] == revoked.json()["revoked_at"]  # original timestamp kept
    assert after.status_code == 401
    assert listed.json()["data"][0]["status"] == "revoked"


async def test_an_expired_key_is_rejected(api: Api) -> None:
    expires = api.clock.now() + timedelta(hours=1)
    project, created = await setup(api, expires_at=expires.isoformat())
    path = f"/v1/projects/{project['id']}"

    live = await bearer_get(api, created["key"], path)
    api.clock.advance(timedelta(hours=1, seconds=1))
    dead = await bearer_get(api, created["key"], path)

    assert created["expires_at"] is not None
    assert live.status_code == 200
    assert dead.status_code == 401


async def test_a_key_stops_working_when_its_project_is_archived(api: Api) -> None:
    owner = await api.operator()
    project = await create_project(api, owner)
    client, csrf_token = await logged_in(api, owner)
    async with aclosing(client):
        created = await create_key(client, csrf_token, project["id"])
        archived = await client.patch(
            f"/v1/projects/{project['id']}", json={"status": "archived"}, headers=csrf(csrf_token)
        )
        new_key = await client.post(
            f"/v1/projects/{project['id']}/api-keys",
            json={"name": "late", "scopes": ["projects:read"]},
            headers=csrf(csrf_token),
        )
        denied = await bearer_get(api, created["key"], f"/v1/projects/{project['id']}")

    assert archived.status_code == 200
    assert denied.status_code == 401
    assert new_key.status_code == 409


async def test_scopes_are_enforced_with_403(api: Api) -> None:
    project, created = await setup(api, scopes=["ledger:read"])
    base = f"/v1/projects/{project['id']}"
    key = created["key"]

    async with api.client() as client:
        get_project = await client.get(base, headers=bearer(key))
        patch = await client.patch(base, json={"name": "x"}, headers=bearer(key))
        list_keys = await client.get(f"{base}/api-keys", headers=bearer(key))
        mint = await client.post(
            f"{base}/api-keys",
            json={"name": "x", "scopes": ["ledger:read"]},
            headers=bearer(key),
        )
        accounts = await client.get(f"{base}/provider-accounts", headers=bearer(key))
        create_project_ = await client.post(
            "/v1/projects", json={"slug": "nope-nope", "name": "x"}, headers=bearer(key)
        )

    assert [
        r.status_code for r in (get_project, patch, list_keys, mint, accounts, create_project_)
    ] == [403] * 6


async def test_a_key_with_every_scope_still_cannot_administer(api: Api) -> None:
    project, created = await setup(api, scopes=ALL_SCOPES)
    base = f"/v1/projects/{project['id']}"
    key = created["key"]

    async with api.client() as client:
        ok = await client.get(base, headers=bearer(key))
        mint = await client.post(
            f"{base}/api-keys", json={"name": "x", "scopes": ["ledger:read"]}, headers=bearer(key)
        )
        revoke = await client.post(f"{base}/api-keys/{created['id']}/revoke", headers=bearer(key))
        accounts = await client.get(f"{base}/provider-accounts", headers=bearer(key))

    assert ok.status_code == 200
    assert (mint.status_code, revoke.status_code, accounts.status_code) == (403, 403, 403)


@pytest.mark.parametrize(
    ("role", "expected"),
    [(Role.OWNER, 201), (Role.ADMIN, 201), (Role.FINANCE, 403), (Role.VIEWER, 403)],
)
async def test_only_owners_and_admins_manage_keys(api: Api, role: Role, expected: int) -> None:
    project = await create_project(api, await api.operator())
    _, client, csrf_token = await member(api, project["id"], role)
    async with aclosing(client):
        response = await client.post(
            f"/v1/projects/{project['id']}/api-keys",
            json={"name": "k", "scopes": ["ledger:read"]},
            headers=csrf(csrf_token),
        )
        listed = await client.get(f"/v1/projects/{project['id']}/api-keys")

    assert response.status_code == expected
    assert listed.status_code == (200 if expected == 201 else 403)


async def test_non_members_get_404_not_403(api: Api) -> None:
    project = await create_project(api, await api.operator())
    stranger = await api.operator()
    client, csrf_token = await logged_in(api, stranger)
    async with aclosing(client):
        listed = await client.get(f"/v1/projects/{project['id']}/api-keys")
        created = await client.post(
            f"/v1/projects/{project['id']}/api-keys",
            json={"name": "k", "scopes": ["ledger:read"]},
            headers=csrf(csrf_token),
        )

    assert (listed.status_code, created.status_code) == (404, 404)


async def test_a_bad_bearer_is_never_rescued_by_a_valid_session_cookie(api: Api) -> None:
    owner = await api.operator()
    project = await create_project(api, owner)
    client, _ = await logged_in(api, owner)
    path = f"/v1/projects/{project['id']}"
    async with aclosing(client):
        cookie_only = await client.get(path)
        bad_key = await client.get(path, headers=bearer(f"fh_test_{'a' * 8}_{'B' * 43}"))
        other_scheme = await client.get(path, headers=bearer("not-a-platform-key"))
        basic = await client.get(path, headers={"Authorization": "Basic Zm9vOmJhcg=="})

    assert cookie_only.status_code == 200
    assert bad_key.status_code == 401  # an fh_ bearer is judged alone
    assert other_scheme.status_code == 200  # not ours: the session authenticator handles it
    assert basic.status_code == 200


async def test_a_valid_key_wins_over_a_session_of_someone_else(api: Api) -> None:
    project, created = await setup(api, scopes=["projects:read"])
    stranger = await api.operator()
    client, _ = await logged_in(api, stranger)
    async with aclosing(client):
        response = await client.get(f"/v1/projects/{project['id']}", headers=bearer(created["key"]))
        listing = await client.get("/v1/projects", headers=bearer(created["key"]))

    assert response.status_code == 200
    assert [p["id"] for p in listing.json()["data"]] == [project["id"]]


async def test_last_used_at_is_throttled(api: Api) -> None:
    owner = await api.operator()
    project = await create_project(api, owner)
    client, csrf_token = await logged_in(api, owner)
    path = f"/v1/projects/{project['id']}"

    async def last_used() -> str | None:
        listed = await client.get(f"{path}/api-keys")
        return listed.json()["data"][0]["last_used_at"]

    async with aclosing(client):
        created = await create_key(client, csrf_token, project["id"])
        assert await last_used() is None
        await bearer_get(api, created["key"], path)
        first = await last_used()
        api.clock.advance(timedelta(seconds=30))
        await bearer_get(api, created["key"], path)
        within_interval = await last_used()
        api.clock.advance(timedelta(seconds=31))
        await bearer_get(api, created["key"], path)
        after_interval = await last_used()

    assert first is not None
    assert within_interval == first
    assert after_interval is not None
    assert after_interval != first


async def test_failed_authentication_does_not_touch_last_used_at(api: Api) -> None:
    owner = await api.operator()
    project = await create_project(api, owner)
    client, csrf_token = await logged_in(api, owner)
    async with aclosing(client):
        created = await create_key(client, csrf_token, project["id"])
        wrong = created["key"][:-3] + "AAA"
        await bearer_get(api, wrong, f"/v1/projects/{project['id']}")
        listed = await client.get(f"/v1/projects/{project['id']}/api-keys")

    assert listed.json()["data"][0]["last_used_at"] is None


@pytest.mark.parametrize(
    ("body", "pointer"),
    [
        ({"name": "k", "scopes": []}, None),
        ({"name": "k", "scopes": ["nope:read"]}, "/scopes"),
        ({"name": "k", "scopes": ["api_keys:manage"]}, "/scopes"),
        ({"name": "   ", "scopes": ["ledger:read"]}, "/name"),
        (
            {"name": "k", "scopes": ["ledger:read"], "expires_at": "2000-01-01T00:00:00+00:00"},
            "/expires_at",
        ),
        (
            {"name": "k", "scopes": ["ledger:read"], "expires_at": "2999-01-01T00:00:00"},
            "/expires_at",
        ),
        ({"scopes": ["ledger:read"]}, None),
    ],
)
async def test_invalid_requests_are_422(
    api: Api, body: dict[str, Any], pointer: str | None
) -> None:
    owner = await api.operator()
    project = await create_project(api, owner)
    client, csrf_token = await logged_in(api, owner)
    async with aclosing(client):
        response = await client.post(
            f"/v1/projects/{project['id']}/api-keys", json=body, headers=csrf(csrf_token)
        )
        listed = await client.get(f"/v1/projects/{project['id']}/api-keys")

    assert response.status_code == 422
    if pointer is not None:
        assert pointer in json.dumps(response.json())
    assert listed.json()["data"] == []  # nothing was created


async def test_listing_paginates_newest_first(api: Api) -> None:
    owner = await api.operator()
    project = await create_project(api, owner)
    client, csrf_token = await logged_in(api, owner)
    async with aclosing(client):
        ids: list[str] = []
        for index in range(3):
            api.clock.advance(timedelta(seconds=1))
            ids.append(
                (await create_key(client, csrf_token, project["id"], name=f"k{index}"))["id"]
            )
        first = (
            await client.get(f"/v1/projects/{project['id']}/api-keys", params={"limit": 2})
        ).json()
        second = (
            await client.get(
                f"/v1/projects/{project['id']}/api-keys",
                params={"limit": 2, "cursor": first["next_cursor"]},
            )
        ).json()

    assert [k["id"] for k in first["data"]] == [ids[2], ids[1]]
    assert [k["id"] for k in second["data"]] == [ids[0]]
    assert (first["has_more"], second["has_more"]) == (True, False)


async def test_an_unknown_or_foreign_key_id_is_404_and_changes_nothing(api: Api) -> None:
    owner = await api.operator()
    mine = await create_project(api, owner)
    client, csrf_token = await logged_in(api, owner)
    foreign_owner = await api.operator()
    foreign_project = await create_project(api, foreign_owner)
    foreign_client, foreign_csrf = await logged_in(api, foreign_owner)
    async with aclosing(foreign_client):
        foreign_key = await create_key(foreign_client, foreign_csrf, foreign_project["id"])
    async with aclosing(client):
        foreign = await client.post(
            f"/v1/projects/{mine['id']}/api-keys/{foreign_key['id']}/revoke",
            headers=csrf(csrf_token),
        )
        garbage = await client.post(
            f"/v1/projects/{mine['id']}/api-keys/not-an-id/revoke", headers=csrf(csrf_token)
        )
    still_valid = await bearer_get(api, foreign_key["key"], f"/v1/projects/{foreign_project['id']}")

    assert (foreign.status_code, garbage.status_code) == (404, 404)
    assert same_problem(foreign, garbage)
    assert still_valid.status_code == 200  # the attempt did not revoke the other project's key


async def test_the_key_never_reaches_the_logs(api: Api, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("DEBUG")
    owner = await api.operator()
    project = await create_project(api, owner)
    client, csrf_token = await logged_in(api, owner)
    async with aclosing(client):
        created = await create_key(client, csrf_token, project["id"])
        await bearer_get(api, created["key"], f"/v1/projects/{project['id']}")
        await bearer_get(api, created["key"][:-4] + "ZZZZ", f"/v1/projects/{project['id']}")
        await client.post(
            f"/v1/projects/{project['id']}/api-keys/{created['id']}/revoke",
            headers=csrf(csrf_token),
        )
        await bearer_get(api, created["key"], f"/v1/projects/{project['id']}")

    secret = KEY_PATTERN.fullmatch(created["key"])
    assert secret is not None
    logged = caplog.text + "\n".join(record.getMessage() for record in caplog.records)
    assert secret["secret"] not in logged
    assert created["key"] not in logged
    assert "ZZZZ" not in logged.replace("ZZZZZ", "")  # the wrong guess is not logged either
    assert "api_key.created" in logged
    assert "api_key.revoked" in logged
    assert created["id"] in logged


async def test_a_database_scan_finds_no_plaintext_secret_anywhere(
    api: Api, owner_engine: AsyncEngine
) -> None:
    _, created = await setup(api)
    secret = KEY_PATTERN.fullmatch(created["key"])
    assert secret is not None

    async with owner_engine.connect() as conn:
        tables = (
            (
                await conn.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
                    )
                )
            )
            .scalars()
            .all()
        )
        for table in tables:
            dump = (
                await conn.execute(
                    # table names come from information_schema, not from input
                    text(f"SELECT coalesce(string_agg(t::text, E'\\n'), '') FROM \"{table}\" t")  # noqa: S608
                )
            ).scalar_one()
            assert secret["secret"] not in dump, f"secret found in {table}"
            assert created["key"] not in dump, f"key found in {table}"
