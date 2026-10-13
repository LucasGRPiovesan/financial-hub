"""Database-level guarantees for ``api_keys`` and ``provider_accounts`` (runtime role ``fh_app``).

The application layer is not the only line of defence: even a compromised or buggy runtime cannot
delete these rows, rewrite what a key is allowed to do, move a credential to another account, or
bring a revoked key back.
"""

from contextlib import aclosing
from datetime import UTC, datetime, timedelta

import psycopg.errors as pg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from financial_hub.shared.ids import IdPrefix, new_uuid, parse_id
from tests.integration.identity.support import Api, csrf, logged_in
from tests.integration.identity.test_privileges import statement_fails
from tests.integration.projects.helpers import create_key, create_project


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM api_keys",
        "DELETE FROM provider_accounts",
        "TRUNCATE api_keys",
        "TRUNCATE provider_accounts",
        *(
            f"UPDATE api_keys SET {column} = {column} WHERE false"  # noqa: S608 - fixed names
            for column in (
                "id",
                "project_id",
                "name",
                "environment",
                "prefix",
                "secret_hash",
                "scopes",
                "created_by",
                "created_at",
                "expires_at",
            )
        ),
        *(
            f"UPDATE provider_accounts SET {column} = {column} WHERE false"  # noqa: S608
            for column in ("id", "project_id", "provider", "environment", "created_at")
        ),
    ],
)
async def test_runtime_role_cannot(app_engine: AsyncEngine, sql: str) -> None:
    await statement_fails(app_engine, sql)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE api_keys SET last_used_at = now() WHERE false",
        "UPDATE api_keys SET revoked_at = now() WHERE false",
        *(
            f"UPDATE provider_accounts SET {column} = {column} WHERE false"  # noqa: S608
            for column in (
                "name",
                "status",
                "key_version",
                "nonce",
                "ciphertext",
                "credentials_hint",
                "updated_at",
            )
        ),
    ],
)
async def test_runtime_role_can_make_the_updates_the_service_needs(
    app_engine: AsyncEngine, sql: str
) -> None:
    async with app_engine.begin() as conn:
        await conn.execute(text(sql))


async def test_a_revoked_key_cannot_be_brought_back(api: Api, app_engine: AsyncEngine) -> None:
    owner = await api.operator()
    project = await create_project(api, owner)
    client, csrf_token = await logged_in(api, owner)
    async with aclosing(client):
        created = await create_key(client, csrf_token, project["id"])
        await client.post(
            f"/v1/projects/{project['id']}/api-keys/{created['id']}/revoke",
            headers=csrf(csrf_token),
        )
    key_id = parse_id(created["id"], IdPrefix.API_KEY)

    async def attempt(statement: str) -> None:
        async with app_engine.connect() as conn:
            await conn.begin()
            with pytest.raises(DBAPIError) as info:
                await conn.execute(text(statement), {"id": key_id})
            await conn.rollback()
        assert isinstance(info.value.orig, pg.IntegrityConstraintViolation), info.value.orig

    await attempt("UPDATE api_keys SET revoked_at = NULL WHERE id = :id")
    await attempt("UPDATE api_keys SET revoked_at = now() + interval '1 day' WHERE id = :id")
    async with app_engine.connect() as conn:
        revoked_at = (
            await conn.execute(
                text("SELECT revoked_at FROM api_keys WHERE id = :id"), {"id": key_id}
            )
        ).scalar_one()
    assert revoked_at is not None
    # writing the same instant again is harmless (a repeated revoke is idempotent)
    async with app_engine.begin() as conn:
        await conn.execute(
            text("UPDATE api_keys SET revoked_at = :at WHERE id = :id"),
            {"at": revoked_at, "id": key_id},
        )


async def test_the_database_refuses_malformed_ciphertext_columns(
    api: Api, owner_engine: AsyncEngine
) -> None:
    owner = await api.operator()
    project = await create_project(api, owner)
    now = datetime.now(UTC)

    async def insert(nonce: bytes, ciphertext: bytes) -> None:
        async with owner_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO provider_accounts (id, project_id, provider, environment, name, "
                    "status, key_version, nonce, ciphertext, created_at, updated_at) VALUES "
                    "(:id, :p, 'asaas', 'sandbox', :name, 'active', 1, :n, :c, :t, :t)"
                ),
                {
                    "id": new_uuid(),
                    "p": parse_id(project["id"], IdPrefix.PROJECT),
                    "name": f"bad-{new_uuid()}",
                    "n": nonce,
                    "c": ciphertext,
                    "t": now + timedelta(seconds=1),
                },
            )

    with pytest.raises(DBAPIError) as short_nonce:
        await insert(b"\x00" * 11, b"\x01" * 40)
    with pytest.raises(DBAPIError) as short_ciphertext:
        await insert(b"\x00" * 12, b"\x01" * 16)  # a tag and nothing else
    await insert(b"\x00" * 12, b"\x01" * 17)  # the smallest valid shape

    assert isinstance(short_nonce.value.orig, pg.CheckViolation)
    assert isinstance(short_ciphertext.value.orig, pg.CheckViolation)
