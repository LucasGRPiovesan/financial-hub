"""Migration ``0005_audit``: 0004 <-> 0005 round trip, on a scratch database.

Same approach as ``test_migration_0004``: the shared development database is never downgraded; the
revisions run against a throw-away database of the same cluster, through the real alembic CLI.
"""

from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from financial_hub.platform.settings import get_settings
from tests.integration.projects.test_migration_0004 import alembic, scratch_url

PREVIOUS = "0004_api_keys_providers"
REVISION = "0005_audit"
FUNCTION = "public.audit_reject_change()"


@pytest.fixture
async def scratch() -> AsyncIterator[AsyncEngine]:
    name = f"fh_migration_{uuid4().hex[:12]}"
    admin = create_async_engine(
        make_url(get_settings().database_url.get_secret_value()), isolation_level="AUTOCOMMIT"
    )
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_async_engine(scratch_url(name))
    try:
        yield engine
    finally:
        await engine.dispose()
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


async def scalar(engine: AsyncEngine, sql: str) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql))).scalar_one()


async def state(engine: AsyncEngine) -> tuple[bool, bool, int, str]:
    return (
        await scalar(engine, "SELECT to_regclass('public.audit_records') IS NOT NULL"),
        await scalar(engine, f"SELECT to_regprocedure('{FUNCTION}') IS NOT NULL"),
        await scalar(
            engine,
            "SELECT count(*) FROM pg_trigger WHERE tgrelid = to_regclass('public.audit_records') "
            "AND NOT tgisinternal",
        ),
        await scalar(engine, "SELECT version_num FROM alembic_version"),
    )


async def test_0005_applies_reverts_and_applies_again(scratch: AsyncEngine) -> None:
    url = scratch.url

    await alembic(url, "upgrade", PREVIOUS)
    assert await state(scratch) == (False, False, 0, PREVIOUS)

    await alembic(url, "upgrade", REVISION)
    assert await state(scratch) == (True, True, 2, REVISION)

    await alembic(url, "downgrade", PREVIOUS)
    assert await state(scratch) == (False, False, 0, PREVIOUS)

    await alembic(url, "upgrade", REVISION)
    assert await state(scratch) == (True, True, 2, REVISION)


async def test_0005_grants_the_runtime_role_insert_and_select_only(scratch: AsyncEngine) -> None:
    await alembic(scratch.url, "upgrade", REVISION)

    async def privilege(name: str) -> bool:
        return bool(
            await scalar(
                scratch, f"SELECT has_table_privilege('fh_app', 'audit_records', '{name}')"
            )
        )

    assert await privilege("SELECT")
    assert await privilege("INSERT")
    for denied in ("UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
        assert not await privilege(denied)


async def test_downgrade_keeps_the_earlier_tables(scratch: AsyncEngine) -> None:
    await alembic(scratch.url, "upgrade", REVISION)
    await alembic(scratch.url, "downgrade", PREVIOUS)

    kept = await scalar(
        scratch,
        "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' "
        "AND table_name IN ('projects', 'api_keys', 'provider_accounts')",
    )
    assert kept == 3
