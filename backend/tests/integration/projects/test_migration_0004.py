"""Migration ``0004_api_keys_providers``: 0003 <-> 0004 round trip, on a scratch database.

The shared development database is never downgraded (other suites run against it): the revisions are
applied to a throw-away database of the same cluster, through the real ``alembic`` CLI.
"""

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from financial_hub.platform.settings import get_settings

BACKEND = Path(__file__).resolve().parents[3]
PREVIOUS = "0003_identity"
REVISION = "0004_api_keys_providers"
TABLES = ("api_keys", "provider_accounts")
TRIGGER_FUNCTION = "public.api_keys_revocation_is_final()"


def scratch_url(name: str) -> URL:
    return make_url(get_settings().database_url.get_secret_value()).set(database=name)


async def alembic(url: URL, *arguments: str) -> None:
    env = {**os.environ, "FH_DATABASE_URL": url.render_as_string(hide_password=False)}
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", *arguments],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"alembic {' '.join(arguments)} failed:\n{result.stderr[-3000:]}"


@pytest.fixture
async def scratch() -> AsyncIterator[AsyncEngine]:
    name = f"fh_migration_{uuid4().hex[:12]}"
    admin = create_async_engine(
        make_url(get_settings().database_url.get_secret_value()),
        isolation_level="AUTOCOMMIT",
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


async def present_tables(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = ANY(:names)"
            ),
            {"names": list(TABLES)},
        )
        return {name for (name,) in rows.all()}


async def trigger_function_exists(engine: AsyncEngine) -> bool:
    async with engine.connect() as conn:
        found = await conn.execute(
            text("SELECT to_regprocedure(:name)"), {"name": TRIGGER_FUNCTION}
        )
        return found.scalar_one() is not None


async def current_revision(engine: AsyncEngine) -> str:
    async with engine.connect() as conn:
        return (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()


async def app_privilege(engine: AsyncEngine, table: str, privilege: str) -> bool:
    """Whole-table privilege; INSERT is granted per column, so it is checked at column level."""
    function = "has_any_column_privilege" if privilege == "INSERT" else "has_table_privilege"
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text(f"SELECT {function}('fh_app', :t, :p)"), {"t": table, "p": privilege}
            )
        ).scalar_one()


async def app_column_privilege(engine: AsyncEngine, table: str, column: str) -> bool:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT has_column_privilege('fh_app', :t, :c, 'UPDATE')"),
                {"t": table, "c": column},
            )
        ).scalar_one()


async def test_0004_applies_reverts_and_applies_again(scratch: AsyncEngine) -> None:
    url = scratch.url

    await alembic(url, "upgrade", PREVIOUS)
    assert await present_tables(scratch) == set()
    assert await current_revision(scratch) == PREVIOUS

    await alembic(url, "upgrade", REVISION)
    assert await present_tables(scratch) == set(TABLES)
    assert await trigger_function_exists(scratch)
    assert await current_revision(scratch) == REVISION

    await alembic(url, "downgrade", PREVIOUS)
    assert await present_tables(scratch) == set()
    assert not await trigger_function_exists(scratch)
    assert await current_revision(scratch) == PREVIOUS

    await alembic(url, "upgrade", REVISION)
    assert await present_tables(scratch) == set(TABLES)
    assert await trigger_function_exists(scratch)
    assert await current_revision(scratch) == REVISION


async def test_0004_grants_the_runtime_role_the_least_it_needs(scratch: AsyncEngine) -> None:
    await alembic(scratch.url, "upgrade", REVISION)

    for table in TABLES:
        assert await app_privilege(scratch, table, "SELECT")
        assert await app_privilege(scratch, table, "INSERT")
        assert not await app_privilege(scratch, table, "DELETE")
        assert not await app_privilege(scratch, table, "TRUNCATE")
    assert await app_column_privilege(scratch, "api_keys", "revoked_at")
    assert await app_column_privilege(scratch, "api_keys", "last_used_at")
    assert not await app_column_privilege(scratch, "api_keys", "secret_hash")
    assert not await app_column_privilege(scratch, "api_keys", "scopes")
    assert await app_column_privilege(scratch, "provider_accounts", "ciphertext")
    assert not await app_column_privilege(scratch, "provider_accounts", "provider")
    assert not await app_column_privilege(scratch, "provider_accounts", "project_id")


async def test_downgrade_keeps_the_earlier_tables(scratch: AsyncEngine) -> None:
    await alembic(scratch.url, "upgrade", REVISION)
    await alembic(scratch.url, "downgrade", PREVIOUS)

    async with scratch.connect() as conn:
        kept = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' "
                    "AND table_name IN ('projects', 'operators', 'sessions', 'project_memberships')"
                )
            )
        ).scalar_one()
    assert kept == 4
