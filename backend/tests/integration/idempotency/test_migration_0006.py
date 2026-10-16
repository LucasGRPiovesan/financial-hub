"""Migration ``0006_idempotency_rate_limits``: 0005 <-> 0006 round trip, on a scratch database."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.integration.projects.test_migration_0004 import (
    alembic,
    app_privilege,
    current_revision,
    scratch,
)

__all__ = ["scratch"]

PREVIOUS = "0005_audit"
REVISION = "0006_idempotency_rate_limits"
TABLES = ("idempotency_records", "rate_limit_buckets")


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


async def test_0006_applies_reverts_and_applies_again(scratch: AsyncEngine) -> None:
    url = scratch.url

    await alembic(url, "upgrade", PREVIOUS)
    assert await present_tables(scratch) == set()

    await alembic(url, "upgrade", REVISION)
    assert await present_tables(scratch) == set(TABLES)
    assert await current_revision(scratch) == REVISION

    await alembic(url, "downgrade", PREVIOUS)
    assert await present_tables(scratch) == set()
    assert await current_revision(scratch) == PREVIOUS

    await alembic(url, "upgrade", REVISION)
    assert await present_tables(scratch) == set(TABLES)


async def test_0006_grants_the_runtime_role_read_write_delete_only(scratch: AsyncEngine) -> None:
    await alembic(scratch.url, "upgrade", REVISION)

    for table in TABLES:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert await app_privilege(scratch, table, privilege), (table, privilege)
        assert not await app_privilege(scratch, table, "TRUNCATE")
