"""Migration ``0010_metering``: 0009 <-> 0010 round trip, on a scratch database.

Same approach as the 0004 and 0009 tests: the revisions run through the real ``alembic`` CLI against
a throw-away database of the cluster, never against the shared development database.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.integration.projects.test_migration_0004 import (  # noqa: F401  (fixture re-export)
    alembic,
    app_privilege,
    current_revision,
    scratch,
)

PREVIOUS = "0009_payments"
REVISION = "0010_metering"
TABLES = ("billable_metrics", "usage_events")
FUNCTIONS = (
    "public.billable_metrics_guard()",
    "public.usage_events_same_project()",
    "public.usage_events_reject_change()",
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
    async with engine.connect() as conn:
        found: set[str] = set()
        for name in FUNCTIONS:
            exists = await conn.execute(text("SELECT to_regprocedure(:name)"), {"name": name})
            if exists.scalar_one() is not None:
                found.add(name)
        return found


async def test_0010_applies_reverts_and_applies_again(scratch: AsyncEngine) -> None:  # noqa: F811
    url = scratch.url

    await alembic(url, "upgrade", PREVIOUS)
    assert await present_tables(scratch) == set()
    assert await current_revision(scratch) == PREVIOUS

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


async def test_0010_usage_is_append_only_and_exact(scratch: AsyncEngine) -> None:  # noqa: F811
    await alembic(scratch.url, "upgrade", REVISION)

    assert await app_privilege(scratch, "usage_events", "SELECT")
    assert await app_privilege(scratch, "usage_events", "INSERT")
    for privilege in ("UPDATE", "DELETE", "TRUNCATE"):
        assert not await app_privilege(scratch, "usage_events", privilege)
    assert await app_privilege(scratch, "billable_metrics", "SELECT")
    assert await app_privilege(scratch, "billable_metrics", "INSERT")
    assert not await app_privilege(scratch, "billable_metrics", "DELETE")
    async with scratch.connect() as conn:
        types = {
            (row.table_name, row.column_name): (row.data_type, row.numeric_scale)
            for row in (
                await conn.execute(
                    text(
                        "SELECT table_name, column_name, data_type, numeric_scale "
                        "FROM information_schema.columns WHERE table_schema = 'public' "
                        "AND table_name = ANY(:names)"
                    ),
                    {"names": list(TABLES)},
                )
            ).all()
        }
        mutable = (
            await conn.execute(
                text(
                    "SELECT "
                    "has_column_privilege('fh_app', 'billable_metrics', 'status', 'UPDATE'), "
                    "has_column_privilege('fh_app', 'billable_metrics', 'aggregation', 'UPDATE'), "
                    "has_column_privilege('fh_app', 'billable_metrics', 'field', 'UPDATE'), "
                    "has_column_privilege('fh_app', 'billable_metrics', 'code', 'UPDATE')"
                )
            )
        ).one()
    assert types[("usage_events", "value")] == ("numeric", 9)
    assert not [name for name, (kind, _) in types.items() if kind in ("real", "double precision")]
    assert tuple(mutable) == (True, False, False, False)


async def test_downgrade_keeps_the_earlier_tables(scratch: AsyncEngine) -> None:  # noqa: F811
    await alembic(scratch.url, "upgrade", REVISION)
    await alembic(scratch.url, "downgrade", PREVIOUS)

    async with scratch.connect() as conn:
        kept = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' "
                    "AND table_name IN ('customer_identities', 'payment_intents', 'events')"
                )
            )
        ).scalar_one()
    assert kept == 3
