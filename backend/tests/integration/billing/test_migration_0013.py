"""Migration ``0013_billing``: 0012 <-> 0013 round trip, on a scratch database.

Same approach as the 0004 test: the revisions run through the real ``alembic`` CLI against a
throw-away database of the cluster, never against the shared development database. Later
revisions exist, so every step names its revision explicitly.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.integration.projects.test_migration_0004 import (  # noqa: F401  (fixture re-export)
    alembic,
    app_privilege,
    current_revision,
    scratch,
)

PREVIOUS = "0012_webhooks"
REVISION = "0013_billing"
TABLES = (
    "prices",
    "price_versions",
    "plans",
    "plan_prices",
    "subscriptions",
    "invoices",
    "invoice_lines",
)
FUNCTIONS = (
    "public.price_versions_guard()",
    "public.subscriptions_guard()",
    "public.invoices_guard()",
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
        found = {
            name: (
                await conn.execute(text("SELECT to_regprocedure(:name)"), {"name": name})
            ).scalar_one()
            for name in FUNCTIONS
        }
    return {name for name, oid in found.items() if oid is not None}


async def has_invoice_link(engine: AsyncEngine) -> bool:
    async with engine.connect() as conn:
        found = await conn.execute(
            text(
                "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'public' "
                "AND table_name = 'payment_intents' AND column_name = 'invoice_id'"
            )
        )
        return found.scalar_one() == 1


async def column_privilege(engine: AsyncEngine, table: str, column: str, privilege: str) -> bool:
    async with engine.connect() as conn:
        granted = await conn.execute(
            text("SELECT has_column_privilege('fh_app', :table, :column, :privilege)"),
            {"table": f"public.{table}", "column": column, "privilege": privilege},
        )
        return bool(granted.scalar_one())


async def test_0013_applies_reverts_and_applies_again(scratch: AsyncEngine) -> None:  # noqa: F811
    url = scratch.url

    await alembic(url, "upgrade", PREVIOUS)
    assert await present_tables(scratch) == set()
    assert not await has_invoice_link(scratch)

    await alembic(url, "upgrade", REVISION)
    assert await present_tables(scratch) == set(TABLES)
    assert await present_functions(scratch) == set(FUNCTIONS)
    assert await has_invoice_link(scratch)
    assert await current_revision(scratch) == REVISION

    await alembic(url, "downgrade", PREVIOUS)
    assert await present_tables(scratch) == set()
    assert await present_functions(scratch) == set()
    assert not await has_invoice_link(scratch)
    assert await current_revision(scratch) == PREVIOUS

    await alembic(url, "upgrade", REVISION)
    assert await present_tables(scratch) == set(TABLES)
    assert await has_invoice_link(scratch)
    assert await current_revision(scratch) == REVISION


async def test_0013_grants_no_delete_and_keeps_money_columns_immutable(
    scratch: AsyncEngine,  # noqa: F811
) -> None:
    await alembic(scratch.url, "upgrade", REVISION)

    for table in TABLES:
        assert await app_privilege(scratch, table, "SELECT")
        assert await app_privilege(scratch, table, "INSERT")
        assert not await app_privilege(scratch, table, "DELETE")
        assert not await app_privilege(scratch, table, "TRUNCATE")
    # an invoice's amounts and lines never change once written; only its status moves
    for column in ("total", "credits_applied", "amount_due", "currency", "period_start"):
        assert not await column_privilege(scratch, "invoices", column, "UPDATE"), column
    assert await column_privilege(scratch, "invoices", "status", "UPDATE")
    for column in ("amount", "quantity", "model", "price_version"):
        assert not await column_privilege(scratch, "invoice_lines", column, "UPDATE"), column
    assert not await column_privilege(scratch, "price_versions", "model", "UPDATE")
    assert await column_privilege(scratch, "price_versions", "effective_to", "UPDATE")
    assert not await column_privilege(scratch, "prices", "code", "UPDATE")
    assert not await column_privilege(scratch, "payment_intents", "invoice_id", "UPDATE")
