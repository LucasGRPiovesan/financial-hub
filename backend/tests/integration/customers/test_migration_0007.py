"""Migration ``0007_customers``: 0006 <-> 0007 round trip, on a scratch database.

Same approach as the 0004 test: the revisions run through the real ``alembic`` CLI against a
throw-away database of the cluster, never against the shared development database.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.integration.projects.test_migration_0004 import (  # noqa: F401  (fixture re-export)
    alembic,
    app_privilege,
    current_revision,
    scratch,
)

PREVIOUS = "0006_idempotency_rate_limits"
REVISION = "0007_customers"
TABLES = ("financial_customers", "customer_identities", "provider_customers")
TRIGGER_FUNCTION = "public.provider_customers_same_project()"


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


async def test_0007_applies_reverts_and_applies_again(scratch: AsyncEngine) -> None:  # noqa: F811
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


async def test_0007_grants_the_runtime_role_no_delete_or_truncate(scratch: AsyncEngine) -> None:  # noqa: F811
    await alembic(scratch.url, "upgrade", REVISION)

    for table in TABLES:
        assert await app_privilege(scratch, table, "SELECT")
        assert await app_privilege(scratch, table, "INSERT")
        assert not await app_privilege(scratch, table, "DELETE")
        assert not await app_privilege(scratch, table, "TRUNCATE")
    async with scratch.connect() as conn:
        immutable = (
            await conn.execute(
                text(
                    "SELECT has_column_privilege('fh_app', 'customer_identities', "
                    "'project_id', 'UPDATE'), "
                    "has_column_privilege('fh_app', 'financial_customers', 'id', 'UPDATE')"
                )
            )
        ).one()
    assert tuple(immutable) == (False, False)


async def test_downgrade_keeps_the_earlier_tables(scratch: AsyncEngine) -> None:  # noqa: F811
    await alembic(scratch.url, "upgrade", REVISION)
    await alembic(scratch.url, "downgrade", PREVIOUS)

    async with scratch.connect() as conn:
        kept = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' "
                    "AND table_name IN ('projects', 'provider_accounts', 'idempotency_keys')"
                )
            )
        ).scalar_one()
    assert kept >= 2
