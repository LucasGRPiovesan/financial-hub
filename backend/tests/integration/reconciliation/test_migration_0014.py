"""Migration ``0014_reconciliation``: 0013 <-> 0014 round trip, on a scratch database.

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

PREVIOUS = "0013_billing"
REVISION = "0014_reconciliation"
TABLES = ("reconciliation_runs", "reconciliation_breaks", "reconciliation_adjustments")
FUNCTIONS = (
    "public.reconciliation_breaks_guard()",
    "public.reconciliation_adjustments_guard()",
)


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


async def present_functions(engine: AsyncEngine) -> set[str]:
    found: set[str] = set()
    async with engine.connect() as conn:
        for name in FUNCTIONS:
            exists = await conn.execute(text("SELECT to_regprocedure(:name)"), {"name": name})
            if exists.scalar_one() is not None:
                found.add(name)
    return found


async def test_0014_applies_reverts_and_applies_again(scratch: AsyncEngine) -> None:  # noqa: F811
    url = scratch.url

    await alembic(url, "upgrade", PREVIOUS)
    assert await present_tables(scratch) == set()

    await alembic(url, "upgrade", REVISION)
    assert await present_tables(scratch) == set(TABLES)
    assert await present_functions(scratch) == set(FUNCTIONS)
    assert await current_revision(scratch) == REVISION

    await alembic(url, "downgrade", PREVIOUS)
    assert await present_tables(scratch) == set()
    assert await present_functions(scratch) == set()
    assert await current_revision(scratch) == PREVIOUS

    await alembic(url, "upgrade", REVISION)
    assert await present_tables(scratch) == set(TABLES)
    assert await current_revision(scratch) == REVISION


async def test_0014_grants_no_delete_and_enforces_four_eyes(
    scratch: AsyncEngine,  # noqa: F811
) -> None:
    await alembic(scratch.url, "upgrade", REVISION)

    for table in TABLES:
        assert await app_privilege(scratch, table, "SELECT")
        assert await app_privilege(scratch, table, "INSERT")
        assert not await app_privilege(scratch, table, "DELETE")
        assert not await app_privilege(scratch, table, "TRUNCATE")
    async with scratch.connect() as conn:
        checks = (
            await conn.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid = 'public.reconciliation_adjustments'::regclass "
                    "AND contype = 'c'"
                )
            )
        ).scalars()
        immutable = (
            await conn.execute(
                text(
                    "SELECT has_column_privilege('fh_app', 'reconciliation_adjustments', "
                    "'amount', 'UPDATE'), has_column_privilege('fh_app', "
                    "'reconciliation_adjustments', 'proposed_by', 'UPDATE')"
                )
            )
        ).one()
    assert "ck_reconciliation_adjustments_four_eyes" in set(checks)
    assert tuple(immutable) == (False, False)
