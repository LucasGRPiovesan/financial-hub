"""``seed-demo``'s integration part: v1 active (approved by Carla), v2 a split draft, once."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from financial_hub.cli.demo import DEMO_OPERATORS, DEMO_PROJECT_SLUG
from financial_hub.shared.ids import IdPrefix, format_id
from tests.integration.identity.support import Api
from tests.integration.identity.test_demo_seed import seed

DEMO_MANIFESTS = text(
    "SELECT m.version, m.status, m.four_eyes, m.content -> 'splits' AS splits, "
    "m.created_by, m.approved_by "
    "FROM integration_manifests m JOIN projects p ON p.id = m.project_id "
    "WHERE p.slug = :slug ORDER BY m.version"
)


async def demo_manifests(engine: AsyncEngine) -> list[dict[str, object]]:
    async with engine.connect() as conn:
        rows = await conn.execute(DEMO_MANIFESTS, {"slug": DEMO_PROJECT_SLUG})
        return [dict(row) for row in rows.mappings().all()]


async def operator_id(engine: AsyncEngine, email: str) -> str:
    async with engine.connect() as conn:
        found = await conn.execute(
            text("SELECT id FROM operators WHERE email = :email"), {"email": email}
        )
        return format_id(IdPrefix.OPERATOR, found.scalar_one())


async def test_seed_demo_activates_v1_and_drafts_a_split_change_once(
    api: Api, owner_engine: AsyncEngine
) -> None:
    await seed(api)
    first = await demo_manifests(owner_engine)
    again = await seed(api)
    second = await demo_manifests(owner_engine)
    ana = await operator_id(owner_engine, DEMO_OPERATORS[0].email)
    carla = await operator_id(owner_engine, DEMO_OPERATORS[2].email)

    assert first == second
    v1, v2 = first
    assert (v1["version"], v1["status"], v1["four_eyes"]) == (1, "active", True)
    assert (v1["created_by"], v1["approved_by"]) == (ana, carla)
    assert (v2["version"], v2["status"], v2["approved_by"]) == (2, "draft", None)
    assert v1["splits"] != v2["splits"]
    assert again.manifests_created == 0
    assert not again.changed
