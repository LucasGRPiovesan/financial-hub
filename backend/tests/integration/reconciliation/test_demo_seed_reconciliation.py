"""``seed-demo``'s reconciliation part: one completed run with one open ``fee_mismatch``, once."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from financial_hub.cli.demo import (
    DEMO_FEE_MISMATCH_EXTRA_FEE,
    DEMO_PROJECT_SLUG,
    DEMO_RECONCILIATION_DAY,
)
from tests.integration.identity.support import Api
from tests.integration.identity.test_demo_seed import seed

DEMO_RECONCILIATION = text(
    "SELECT r.id AS run_id, r.status, r.breaks_opened, r.window_start, r.window_end, "
    "b.kind, b.state, b.difference "
    "FROM reconciliation_runs r JOIN projects p ON p.id = r.project_id "
    "LEFT JOIN reconciliation_breaks b ON b.run_id = r.id "
    "WHERE p.slug = :slug AND r.triggered_by = 'seed-demo' ORDER BY r.id, b.id"
)


async def demo_reconciliation(engine: AsyncEngine) -> list[dict[str, object]]:
    async with engine.connect() as conn:
        rows = await conn.execute(DEMO_RECONCILIATION, {"slug": DEMO_PROJECT_SLUG})
        return [dict(row) for row in rows.mappings().all()]


async def test_seed_demo_records_one_run_with_one_open_fee_mismatch(
    api: Api, app_engine: AsyncEngine
) -> None:
    await seed(api)
    first = await demo_reconciliation(app_engine)
    again = await seed(api)
    second = await demo_reconciliation(app_engine)

    assert first == second
    [row] = first
    assert row["status"] == "completed"
    assert row["breaks_opened"] == 1
    assert row["window_start"] == row["window_end"] == DEMO_RECONCILIATION_DAY
    assert (row["kind"], row["state"]) == ("fee_mismatch", "open")
    assert row["difference"] == -DEMO_FEE_MISMATCH_EXTRA_FEE
    assert not again.reconciliation_run_created
    assert not again.changed
