"""Migration ``0009_payments``: 0008 <-> 0009 round trip, on a scratch database.

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

PREVIOUS = "0008_member_management"
REVISION = "0009_payments"
TABLES = ("payment_intents", "provider_payments", "payment_attempts", "refunds")
FUNCTIONS = ("public.payment_intents_same_project()", "public.payments_match_intent()")


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


async def test_0009_applies_reverts_and_applies_again(scratch: AsyncEngine) -> None:  # noqa: F811
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


async def test_0009_grants_no_delete_and_keeps_money_integer(scratch: AsyncEngine) -> None:  # noqa: F811
    await alembic(scratch.url, "upgrade", REVISION)

    for table in TABLES:
        assert await app_privilege(scratch, table, "SELECT")
        assert await app_privilege(scratch, table, "INSERT")
        assert not await app_privilege(scratch, table, "DELETE")
        assert not await app_privilege(scratch, table, "TRUNCATE")
    async with scratch.connect() as conn:
        money_types = {
            row.data_type
            for row in (
                await conn.execute(
                    text(
                        "SELECT data_type FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = ANY(:names) "
                        "AND (column_name LIKE '%amount%' OR column_name LIKE 'fee%')"
                    ),
                    {"names": list(TABLES)},
                )
            ).all()
        }
        immutable = (
            await conn.execute(
                text(
                    "SELECT has_column_privilege('fh_app', 'payment_intents', 'amount', 'UPDATE'), "
                    "has_column_privilege('fh_app', 'payment_intents', 'project_id', 'UPDATE'), "
                    "has_column_privilege('fh_app', 'refunds', 'amount', 'UPDATE')"
                )
            )
        ).one()
    assert money_types == {"bigint"}
    assert tuple(immutable) == (False, False, False)


async def test_downgrade_keeps_the_earlier_tables(scratch: AsyncEngine) -> None:  # noqa: F811
    await alembic(scratch.url, "upgrade", REVISION)
    await alembic(scratch.url, "downgrade", PREVIOUS)

    async with scratch.connect() as conn:
        kept = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' "
                    "AND table_name IN ('customer_identities', 'provider_accounts', 'events')"
                )
            )
        ).scalar_one()
    assert kept == 3
