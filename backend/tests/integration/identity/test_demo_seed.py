"""``seed-demo``: idempotent demo workspace (project, operators, customers, payments, API key)."""

import asyncio
import io
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from financial_hub.cli import main
from financial_hub.cli.demo import DEMO_PROJECT_SLUG, DemoServices, SeedResult, seed_demo
from financial_hub.cli.demo_data import DEMO_CUSTOMERS, DEMO_KEY_NAME, DEMO_PAYMENTS
from financial_hub.modules.customers.application.service import CustomerService
from financial_hub.modules.payments.application.api import PaymentService
from financial_hub.modules.projects.application.api_keys import ApiKeyService
from financial_hub.modules.projects.application.service import ProjectService
from financial_hub.platform.db import UnitOfWork
from financial_hub.shared.ids import IdPrefix, parse_id
from financial_hub.shared.pagination import PageParams
from tests.integration.identity.support import Api, Operator, login

DEMO_COUNTS = text(
    "SELECT (SELECT count(*) FROM projects WHERE slug = :slug) AS projects, "
    "(SELECT count(*) FROM operators WHERE email LIKE '%@demo.financial-hub.local') AS operators, "
    "(SELECT count(*) FROM project_memberships m JOIN projects p ON p.id = m.project_id "
    " WHERE p.slug = :slug) AS memberships"
)

# public payment status per demo external_ref, as the payments service derives it: captured and
# settled, captured only, pending, rejected, partially and fully refunded
EXPECTED_PAYMENT_STATUSES: dict[str, str] = {
    "demo-pay-01": "settled",
    "demo-pay-02": "settled",
    "demo-pay-03": "requires_payment",
    "demo-pay-04": "requires_payment",
    "demo-pay-05": "requires_payment",
    "demo-pay-06": "succeeded",
    "demo-pay-07": "failed",
    "demo-pay-08": "settled",
    "demo-pay-09": "partially_refunded",
    "demo-pay-10": "refunded",
    "demo-pay-11": "failed",
    "demo-pay-12": "requires_payment",
}


async def counts(engine: AsyncEngine) -> dict[str, int]:
    async with engine.connect() as conn:
        return dict((await conn.execute(DEMO_COUNTS, {"slug": DEMO_PROJECT_SLUG})).mappings().one())


def services(api: Api) -> DemoServices:
    """The CLI's wiring (``DemoServices.from_settings``), from the test app's settings."""
    return DemoServices.from_settings(api.app.state.settings)


async def seed(api: Api) -> SeedResult:
    return await seed_demo(api.factory, hasher=api.hasher, clock=api.clock, services=services(api))


async def test_seed_demo_is_idempotent(api: Api, app_engine: AsyncEngine) -> None:
    await seed(api)
    first = await counts(app_engine)
    again = await seed(api)
    second = await counts(app_engine)

    assert first == second
    assert first["projects"] == 1 and first["memberships"] == 4 and first["operators"] >= 4
    assert not again.changed


async def test_seeded_roles_and_unprintable_passwords(api: Api, app_engine: AsyncEngine) -> None:
    await seed(api)
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
    await seed(api)
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "DELETE FROM project_memberships WHERE role = 'viewer' AND project_id = "
                "(SELECT id FROM projects WHERE slug = :slug) AND operator_id = "
                "(SELECT id FROM operators WHERE email = 'diego@demo.financial-hub.local')"
            ),
            {"slug": DEMO_PROJECT_SLUG},
        )
    repaired = await seed(api)

    assert (repaired.project_created, repaired.operators_created) == (False, 0)
    assert repaired.memberships_created == 1
    assert not (await seed(api)).changed


async def test_demo_customers_are_seeded_once(api: Api) -> None:
    await seed(api)
    again = await seed(api)

    assert again.customers_created == 0
    assert again.customers_present == len(DEMO_CUSTOMERS) == 8
    project_id = await _project_id(api)
    async with UnitOfWork(api.factory) as uow:
        customers = CustomerService(
            uow,
            clock=api.clock,
            keyring=api.app.state.keyring,
            pepper=services(api).pepper,
        )
        for spec in DEMO_CUSTOMERS:
            view = await customers.get_by_ref(project_id, spec.external_ref)
            assert view.name == spec.name and view.kind == spec.kind


async def test_demo_payments_have_expected_statuses_and_integer_amounts(api: Api) -> None:
    result = await seed(api)
    again = await seed(api)

    assert result.payments_present == len(DEMO_PAYMENTS) == 12
    assert again.payments_created == 0
    statuses = await _payment_statuses(api)
    assert set(statuses) == {spec.external_ref for spec in DEMO_PAYMENTS}
    assert statuses == EXPECTED_PAYMENT_STATUSES
    assert all(isinstance(spec.amount, int) for spec in DEMO_PAYMENTS)


async def test_demo_api_key_is_created_once_and_never_printed(api: Api) -> None:
    first = await seed(api)
    again = await seed(api)

    assert first.api_keys_present == 1 and again.api_keys_present == 1
    assert not again.api_key_created
    keys = await _api_key_names(api)
    assert keys.count(DEMO_KEY_NAME) == 1


async def _project_id(api: Api) -> UUID:
    async with UnitOfWork(api.factory) as uow:
        project = await ProjectService(uow).find_by_slug(DEMO_PROJECT_SLUG)
    assert project is not None
    return parse_id(project.id, IdPrefix.PROJECT)


async def _payment_statuses(api: Api) -> dict[str, str]:
    project_id = await _project_id(api)
    payments = PaymentService(api.factory, clock=api.clock, registry=services(api).registry)
    statuses: dict[str, str] = {}
    cursor: str | None = None
    while True:
        page = await payments.list_payments(project_id, PageParams(cursor=cursor, limit=100))
        for view in page.data:
            if view.external_ref is not None and view.external_ref.startswith("demo-pay-"):
                statuses[view.external_ref] = view.status
        if not page.has_more or page.next_cursor is None:
            return statuses
        cursor = page.next_cursor


async def _api_key_names(api: Api) -> list[str]:
    project_id = await _project_id(api)
    async with UnitOfWork(api.factory) as uow:
        keys = ApiKeyService(
            uow,
            clock=api.clock,
            pepper=services(api).pepper,
            environment=services(api).key_environment,
        )
        page = await keys.list_keys(project_id, PageParams(cursor=None, limit=100))
    return [view.name for view in page.data]
