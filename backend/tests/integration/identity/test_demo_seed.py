"""``seed-demo``: idempotent demo workspace (project, four operators, four memberships)."""

import asyncio
import io

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from financial_hub.cli import main
from financial_hub.cli.demo import DEMO_PROJECT_SLUG, seed_demo
from tests.integration.identity.support import Api, Operator, login

DEMO_COUNTS = text(
    "SELECT (SELECT count(*) FROM projects WHERE slug = :slug) AS projects, "
    "(SELECT count(*) FROM operators WHERE email LIKE '%@demo.financial-hub.local') AS operators, "
    "(SELECT count(*) FROM project_memberships m JOIN projects p ON p.id = m.project_id "
    " WHERE p.slug = :slug) AS memberships"
)


async def counts(engine: AsyncEngine) -> dict[str, int]:
    async with engine.connect() as conn:
        return dict((await conn.execute(DEMO_COUNTS, {"slug": DEMO_PROJECT_SLUG})).mappings().one())


async def test_seed_demo_is_idempotent(api: Api, app_engine: AsyncEngine) -> None:
    await seed_demo(api.factory, hasher=api.hasher, clock=api.clock)
    first = await counts(app_engine)
    again = await seed_demo(api.factory, hasher=api.hasher, clock=api.clock)
    second = await counts(app_engine)

    assert first == second
    assert first["projects"] == 1 and first["memberships"] == 4 and first["operators"] >= 4
    assert not again.changed


async def test_seeded_roles_and_unprintable_passwords(api: Api, app_engine: AsyncEngine) -> None:
    await seed_demo(api.factory, hasher=api.hasher, clock=api.clock)
    async with app_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT o.display_name, m.role FROM project_memberships m "
                    "JOIN projects p ON p.id = m.project_id "
                    "JOIN operators o ON o.id = m.operator_id "
                    "WHERE p.slug = :slug ORDER BY o.display_name"
                ),
                {"slug": DEMO_PROJECT_SLUG},
            )
        ).all()
    assert [tuple(r) for r in rows] == [
        ("Ana Owner", "owner"),
        ("Bruno Admin", "admin"),
        ("Carla Finance", "finance"),
        ("Diego Viewer", "viewer"),
    ]
    async with api.client() as client:
        guess = await login(client, Operator("", "ana@demo.financial-hub.local", "x"))
    # The seeded operator persists in the shared test DB, so earlier runs may have tripped the
    # per-account throttle: either way a guessed password is rejected.
    assert guess.status_code in (401, 429)


async def test_cli_command_reports_and_is_repeatable() -> None:
    first_out, second_out, err = io.StringIO(), io.StringIO(), io.StringIO()
    code_a = await asyncio.to_thread(main, ["seed-demo"], stdout=first_out, stderr=err)
    code_b = await asyncio.to_thread(main, ["seed-demo"], stdout=second_out, stderr=err)

    assert (code_a, code_b) == (0, 0), err.getvalue()
    assert "demo workspace" in first_out.getvalue()
    assert "already seeded" in second_out.getvalue()
    assert "password" not in (first_out.getvalue() + second_out.getvalue()).lower()


async def test_seed_demo_restores_a_missing_membership(api: Api, owner_engine: AsyncEngine) -> None:
    await seed_demo(api.factory, hasher=api.hasher, clock=api.clock)
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "DELETE FROM project_memberships WHERE role = 'viewer' AND project_id = "
                "(SELECT id FROM projects WHERE slug = :slug) AND operator_id = "
                "(SELECT id FROM operators WHERE email = 'diego@demo.financial-hub.local')"
            ),
            {"slug": DEMO_PROJECT_SLUG},
        )
    repaired = await seed_demo(api.factory, hasher=api.hasher, clock=api.clock)

    assert (repaired.project_created, repaired.operators_created) == (False, 0)
    assert repaired.memberships_created == 1
    assert not (await seed_demo(api.factory, hasher=api.hasher, clock=api.clock)).changed
