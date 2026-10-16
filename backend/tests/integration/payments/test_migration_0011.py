"""Migration ``0011_provider_webhooks``: 0010 <-> 0011 round trip, on a scratch database.

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

PREVIOUS = "0010_metering"
REVISION = "0011_provider_webhooks"
TABLES = ("provider_webhook_events", "provider_customer_provisions")
FUNCTION = "public.provider_inbound_same_project()"


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


async def function_exists(engine: AsyncEngine) -> bool:
    async with engine.connect() as conn:
        found = await conn.execute(text("SELECT to_regprocedure(:name)"), {"name": FUNCTION})
        return found.scalar_one() is not None


async def test_0011_applies_reverts_and_applies_again(scratch: AsyncEngine) -> None:  # noqa: F811
    url = scratch.url

    await alembic(url, "upgrade", PREVIOUS)
    assert await present_tables(scratch) == set()

    await alembic(url, "upgrade", REVISION)
    assert await present_tables(scratch) == set(TABLES)
    assert await function_exists(scratch)
    assert await current_revision(scratch) == REVISION

    await alembic(url, "downgrade", PREVIOUS)
    assert await present_tables(scratch) == set()
    assert not await function_exists(scratch)
    assert await current_revision(scratch) == PREVIOUS

    await alembic(url, "upgrade", REVISION)
    assert await present_tables(scratch) == set(TABLES)
    assert await current_revision(scratch) == REVISION


async def test_0011_grants_no_delete_and_keeps_the_raw_body_immutable(
    scratch: AsyncEngine,  # noqa: F811
) -> None:
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
                    "SELECT "
                    "has_column_privilege('fh_app', 'provider_webhook_events', 'body', 'UPDATE'), "
                    "has_column_privilege('fh_app', 'provider_webhook_events', 'project_id', "
                    "'UPDATE'), "
                    "has_column_privilege('fh_app', 'provider_webhook_events', "
                    "'provider_event_id', 'UPDATE'), "
                    "has_column_privilege('fh_app', 'provider_customer_provisions', 'project_id', "
                    "'UPDATE')"
                )
            )
        ).one()
    assert tuple(immutable) == (False, False, False, False)
