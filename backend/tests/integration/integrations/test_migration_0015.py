"""Migration ``0015_integrations``: 0014 <-> 0015 round trip, on a scratch database.

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

PREVIOUS = "0014_reconciliation"
REVISION = "0015_integrations"
TABLES = (
    "integration_manifests",
    "integration_manifest_activations",
    "integration_webhook_endpoints",
    "business_events",
)
GUARD = "public.integration_manifests_guard()"


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


async def guard_exists(engine: AsyncEngine) -> bool:
    async with engine.connect() as conn:
        found = await conn.execute(text("SELECT to_regprocedure(:name)"), {"name": GUARD})
        return found.scalar_one() is not None


async def test_0015_applies_reverts_and_applies_again(scratch: AsyncEngine) -> None:  # noqa: F811
    url = scratch.url

    await alembic(url, "upgrade", PREVIOUS)
    assert await present_tables(scratch) == set()

    await alembic(url, "upgrade", REVISION)
    assert await present_tables(scratch) == set(TABLES)
    assert await guard_exists(scratch)
    assert await current_revision(scratch) == REVISION

    await alembic(url, "downgrade", PREVIOUS)
    assert await present_tables(scratch) == set()
    assert not await guard_exists(scratch)
    assert await current_revision(scratch) == PREVIOUS

    await alembic(url, "upgrade", REVISION)
    assert await present_tables(scratch) == set(TABLES)
