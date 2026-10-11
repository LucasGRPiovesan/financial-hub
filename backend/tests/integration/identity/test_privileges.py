"""``fh_app`` (the runtime role) cannot delete identity rows or rewrite who/what they belong to."""

import psycopg.errors as pg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine


async def statement_fails(engine: AsyncEngine, sql: str) -> None:
    async with engine.connect() as conn:
        await conn.begin()
        with pytest.raises(DBAPIError) as info:
            await conn.execute(text(sql))
        await conn.rollback()
    assert isinstance(info.value.orig, pg.InsufficientPrivilege), info.value.orig


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM operators",
        "DELETE FROM sessions",
        "DELETE FROM project_memberships",
        "DELETE FROM projects",
        "DELETE FROM login_throttle",
        "TRUNCATE operators",
        "TRUNCATE sessions",
        "UPDATE operators SET email = 'x@example.test' WHERE false",
        "UPDATE operators SET status = 'active' WHERE false",
        "UPDATE operators SET id = id WHERE false",
        "UPDATE sessions SET operator_id = operator_id WHERE false",
        "UPDATE sessions SET csrf_token_hash = csrf_token_hash WHERE false",
        "UPDATE sessions SET absolute_expires_at = absolute_expires_at WHERE false",
        "UPDATE project_memberships SET role = 'owner' WHERE false",
        "UPDATE projects SET slug = slug WHERE false",
        "UPDATE login_throttle SET key_hash = key_hash WHERE false",
    ],
)
async def test_runtime_role_cannot(app_engine: AsyncEngine, sql: str) -> None:
    await statement_fails(app_engine, sql)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE operators SET failed_login_count = 0 WHERE false",
        "UPDATE sessions SET revoked_at = now() WHERE false",
        "UPDATE projects SET name = name WHERE false",
        "UPDATE login_throttle SET attempts = 0 WHERE false",
    ],
)
async def test_runtime_role_can_make_the_updates_the_service_needs(
    app_engine: AsyncEngine, sql: str
) -> None:
    async with app_engine.begin() as conn:
        await conn.execute(text(sql))
