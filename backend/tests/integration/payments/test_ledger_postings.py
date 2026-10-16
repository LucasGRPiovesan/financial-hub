"""Payment transitions post to the ledger in their own transaction (PRO-42, FINANCIAL_MODEL s4).

Real Postgres as ``fh_app`` and the scriptable fake provider: every transition is applied through
the payment service (``refresh`` stands in for a webhook) or the HTTP API, then the journal is read
back. Idempotency is proven by replaying transitions and by calling the hook again directly.
"""

from datetime import timedelta
from typing import Any
from uuid import UUID

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from financial_hub.modules.payments.application.ledger_postings import (
    PaymentChange,
    post_payment_changes,
)
from financial_hub.modules.payments.domain import PaymentState
from financial_hub.modules.payments.infrastructure import repository
from financial_hub.modules.payments.infrastructure.fake_provider import (
    FakeBehavior,
    FakeRefundBehavior,
)
from financial_hub.platform.db import UnitOfWork
from financial_hub.shared.ids import IdPrefix, parse_id
from financial_hub.shared.money import BRL, Money
from tests.integration.identity.support import Api
from tests.integration.ledger.support import Ledger
from tests.integration.payments.helpers import Shop, intent_uuid, shop
from tests.integration.projects.helpers import bearer

LEDGER_SCOPES = ("payments:write", "payments:refund", "ledger:read")

type Entries = dict[str, tuple[str, str]]


async def entries(engine: AsyncEngine, project: UUID) -> Entries:
    """``{idempotency key: (recipe, status)}`` of the project's journal."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT idempotency_key, recipe, status FROM journal_entries "
                "WHERE project_id = :project"
            ),
            {"project": project},
        )
        return {row.idempotency_key: (row.recipe, row.status) for row in rows.all()}


async def balances(engine: AsyncEngine, project: UUID) -> dict[str, tuple[int, int]]:
    """``{account family: (posted Dr - Cr, pending Dr - Cr)}`` summed over owners."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT split_part(code, ':', 1) AS family, "
                "sum(posted_dr - posted_cr) AS posted, sum(pending_dr - pending_cr) AS pending "
                "FROM ledger_accounts WHERE project_id = :project GROUP BY 1"
            ),
            {"project": project},
        )
        return {row.family: (int(row.posted), int(row.pending)) for row in rows.all()}


def posted(found: dict[str, tuple[int, int]]) -> dict[str, int]:
    """Posted balances that are not zero."""
    return {family: value for family, (value, _) in found.items() if value}


async def provider_payment_row(engine: AsyncEngine, intent_id: UUID) -> UUID:
    async with engine.connect() as conn:
        found = await conn.execute(
            text("SELECT id FROM provider_payments WHERE payment_intent_id = :intent"),
            {"intent": intent_id},
        )
        return found.scalar_one()


async def pending_payment(s: Shop, app_engine: AsyncEngine) -> tuple[str, str, UUID, UUID]:
    """A pending fake payment: ``(public id, provider payment id, intent id, provider row id)``."""
    created = await s.create()
    assert created.status_code == 201, created.text
    view = created.json()
    intent_id = intent_uuid(view["id"])
    return (
        view["id"],
        view["provider_payment_id"],
        intent_id,
        await provider_payment_row(app_engine, intent_id),
    )


def capture_keys(intent_id: UUID, row: UUID) -> Entries:
    return {
        f"invoice:{intent_id}:finalized": ("R1", "posted"),
        f"payment:{row}:captured": ("R2", "posted"),
        f"payment:{row}:split": ("R3", "posted"),
    }


async def test_first_capture_books_r1_r2_r3_with_the_fee(api: Api, app_engine: AsyncEngine) -> None:
    async with shop(api) as s:
        _pid, provider_id, intent_id, row = await pending_payment(s, app_engine)
        pending = await entries(app_engine, s.project_uuid)
        s.fake.capture(provider_id, fee=99)
        status = await s.service().refresh(intent_id)
        booked = await entries(app_engine, s.project_uuid)
        found = posted(await balances(app_engine, s.project_uuid))
        again = await s.service().refresh(intent_id)
        replayed = await entries(app_engine, s.project_uuid)
        await Ledger(api.factory).assert_consistent(s.project_uuid)

    assert pending == {}  # nothing to book before the capture
    assert status is not None and status.value == "succeeded" and again == status
    assert booked == capture_keys(intent_id, row) == replayed
    assert found == {"provider_receivable": 901, "provider_fees": 99, "platform_revenue": -1000}
    assert s.confirmations.calls == [(provider_id, False)]


async def test_a_payment_captured_at_creation_books_in_the_creating_transaction(
    api: Api, app_engine: AsyncEngine
) -> None:
    async with shop(api) as s:
        s.fake.script(FakeBehavior.SUCCEED_CAPTURED)
        _pid, _provider_id, intent_id, row = await pending_payment(s, app_engine)
        booked = await entries(app_engine, s.project_uuid)
        found = posted(await balances(app_engine, s.project_uuid))
        await Ledger(api.factory).assert_consistent(s.project_uuid)

    assert booked == capture_keys(intent_id, row)
    assert found == {"provider_receivable": 1000, "platform_revenue": -1000}  # fee 0


async def test_a_late_capture_is_unapplied_cash_only(api: Api, app_engine: AsyncEngine) -> None:
    async with shop(api) as s:
        _pid, provider_id, intent_id, row = await pending_payment(s, app_engine)
        s.fake.fail(provider_id)
        await s.service().refresh(intent_id)
        s.fake.capture(provider_id, fee=50)
        await s.service().refresh(intent_id)
        await s.service().refresh(intent_id)
        booked = await entries(app_engine, s.project_uuid)
        found = posted(await balances(app_engine, s.project_uuid))
        await Ledger(api.factory).assert_consistent(s.project_uuid)

    assert booked == {f"payment:{row}:unapplied": ("R2b", "posted")}  # never R1/R2/R3
    assert found == {
        "provider_receivable": 950,
        "provider_fees": 50,
        "customer_unapplied_cash": -1000,
    }
    assert s.confirmations.calls == [(provider_id, True)]


async def test_a_changed_net_books_a_fee_adjustment_once(api: Api, app_engine: AsyncEngine) -> None:
    async with shop(api) as s:
        pid, provider_id, intent_id, row = await pending_payment(s, app_engine)
        s.fake.capture(provider_id, fee=99)
        await s.service().refresh(intent_id)
        s.fake.capture(provider_id, fee=120)  # the provider now reports net 880
        await s.service().refresh(intent_id)
        await s.service().refresh(intent_id)
        booked = await entries(app_engine, s.project_uuid)
        found = posted(await balances(app_engine, s.project_uuid))
        view = await s.get(f"/{pid}")
        await Ledger(api.factory).assert_consistent(s.project_uuid)

    assert booked == {
        **capture_keys(intent_id, row),
        f"payment:{row}:fee:880": ("R2a", "posted"),
    }
    assert found == {"provider_receivable": 880, "provider_fees": 120, "platform_revenue": -1000}
    assert view.json()["status"] == "succeeded"


async def test_settlement_books_r4_for_the_booked_net(api: Api, app_engine: AsyncEngine) -> None:
    async with shop(api) as s:
        _pid, provider_id, intent_id, row = await pending_payment(s, app_engine)
        s.fake.capture(provider_id, fee=99)
        s.fake.settle(provider_id)
        await s.service().refresh(intent_id)  # capture and settlement in one transition
        await s.service().refresh(intent_id)
        booked = await entries(app_engine, s.project_uuid)
        found = posted(await balances(app_engine, s.project_uuid))
        await Ledger(api.factory).assert_consistent(s.project_uuid)

    assert booked == {**capture_keys(intent_id, row), f"payment:{row}:settled": ("R4", "posted")}
    assert found == {"provider_balance": 901, "provider_fees": 99, "platform_revenue": -1000}


async def test_hub_refunds_hold_then_post_or_archive(api: Api, app_engine: AsyncEngine) -> None:
    async with shop(api) as s:
        pid, provider_id, intent_id, row = await pending_payment(s, app_engine)
        s.fake.capture(provider_id)
        await s.service().refresh(intent_id)
        s.fake.script_refunds(FakeRefundBehavior.SUBMITTED, FakeRefundBehavior.REJECT)
        service = s.service()
        submitted = await service.refund(s.scope(), pid, amount=300, reason=None)
        holding = await entries(app_engine, s.project_uuid)
        held = await balances(app_engine, s.project_uuid)
        denied = await service.refund(s.scope(), pid, amount=100, reason=None)
        snapshot = await s.fake.get_payment(provider_id)
        s.fake.complete_refund(provider_id, snapshot.refunds[0].provider_refund_id)
        await service.refresh(intent_id)
        await service.refresh(intent_id)
        booked = await entries(app_engine, s.project_uuid)
        found = posted(await balances(app_engine, s.project_uuid))
        await Ledger(api.factory).assert_consistent(s.project_uuid)

    first, second = (parse_id(r.id, IdPrefix.REFUND) for r in (submitted, denied))
    assert (submitted.status, denied.status) == ("submitted", "denied")
    assert holding == {**capture_keys(intent_id, row), f"refund:{first}:pending": ("R8", "pending")}
    assert held["platform_revenue"] == (-1000, 300)  # the hold is pending, posted untouched
    assert held["provider_receivable"] == (1000, -300)
    assert booked == {
        **capture_keys(intent_id, row),
        f"refund:{first}:pending": ("R8", "posted"),
        f"refund:{second}:pending": ("R8", "archived"),
    }
    assert found == {"provider_receivable": 700, "platform_revenue": -700}


async def test_a_refund_made_at_the_provider_posts_directly(
    api: Api, app_engine: AsyncEngine
) -> None:
    async with shop(api) as s:
        _pid, provider_id, intent_id, row = await pending_payment(s, app_engine)
        s.fake.capture(provider_id, fee=99)
        s.fake.settle(provider_id)
        await s.service().refresh(intent_id)
        s.fake.refund_externally(provider_id, Money(250, BRL))
        await s.service().refresh(intent_id)
        await s.service().refresh(intent_id)
        booked = await entries(app_engine, s.project_uuid)
        found = posted(await balances(app_engine, s.project_uuid))
        await Ledger(api.factory).assert_consistent(s.project_uuid)

    refunds = {key: value for key, value in booked.items() if key.startswith("refund:")}
    assert list(refunds.values()) == [("R8", "posted")]
    assert len(booked) == 5 and f"payment:{row}:settled" in booked
    assert found == {"provider_balance": 651, "provider_fees": 99, "platform_revenue": -750}


async def test_replaying_every_transition_never_posts_twice(
    api: Api, app_engine: AsyncEngine
) -> None:
    async with shop(api) as s:
        pid, provider_id, intent_id, _row = await pending_payment(s, app_engine)
        s.fake.capture(provider_id, fee=99)
        s.fake.settle(provider_id)
        await s.service().refresh(intent_id)
        await s.service().refund(s.scope(), pid, amount=400, reason=None)
        s.fake.capture(provider_id, fee=80)
        await s.service().refresh(intent_id)
        booked = await entries(app_engine, s.project_uuid)
        before = await balances(app_engine, s.project_uuid)
        for days in (1, 2):  # the whole history again, as if nothing had been recorded
            async with UnitOfWork(api.factory) as uow:
                intent = await repository.find_intent(uow.session, intent_id)
                payment = await repository.get_provider_payment(uow.session, intent_id)
                assert intent is not None and payment is not None
                refunds = await repository.refunds_of(uow.session, [intent_id])
                at = api.clock.now() + timedelta(days=days)
                change = PaymentChange(intent, payment, PaymentState(), at, (), refunds)
                await post_payment_changes(uow, change)
        replayed = await entries(app_engine, s.project_uuid)
        after = await balances(app_engine, s.project_uuid)
        await Ledger(api.factory).assert_consistent(s.project_uuid)

    assert sorted(recipe for recipe, _ in booked.values()) == [
        "R1",
        "R2",
        "R2a",
        "R3",
        "R4",
        "R8",
    ]
    assert replayed == booked and after == before


async def test_capture_settle_and_partial_refund_balance_through_the_http_api(
    api: Api,
) -> None:
    async with shop(api, scopes=LEDGER_SCOPES) as s:
        created = await s.create()
        assert created.status_code == 201, created.text
        payment = created.json()
        intent_id = intent_uuid(payment["id"])
        s.fake.capture(payment["provider_payment_id"], fee=99)
        await s.service().refresh(intent_id)
        s.fake.settle(payment["provider_payment_id"])
        await s.service().refresh(intent_id)
        refund = await s.refund(payment["id"], amount=300)
        after = (await s.get(f"/{payment['id']}")).json()
        async with api.client() as client:
            trial = await client.get(
                f"/v1/projects/{s.project_id}/ledger/trial-balance", headers=bearer(s.key)
            )
            journal = await client.get(
                f"/v1/projects/{s.project_id}/ledger/entries", headers=bearer(s.key)
            )

    assert refund.status_code == 201, refund.text
    assert (after["status"], after["refunded_amount"]) == ("partially_refunded", 300)
    assert trial.status_code == 200, trial.text
    body: dict[str, Any] = trial.json()
    assert body["balanced"] is True
    assert [(t["currency"], t["debits"] == t["credits"]) for t in body["totals"]] == [("BRL", True)]
    assert journal.status_code == 200, journal.text
    recipes = sorted(entry["recipe"] for entry in journal.json()["data"])
    assert recipes == ["R1", "R2", "R3", "R4", "R8"]


# --- property: random capture / fee / settle / refund sequences --------------------------------

STEP = st.one_of(
    st.tuples(st.just("capture"), st.integers(min_value=0, max_value=1000)),
    st.tuples(st.just("settle"), st.just(0)),
    st.tuples(st.just("refund"), st.integers(min_value=1, max_value=1200)),
    st.tuples(st.just("external_refund"), st.integers(min_value=1, max_value=1000)),
)


@settings(
    max_examples=15,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(steps=st.lists(STEP, min_size=1, max_size=6))
async def test_random_sequences_keep_the_ledger_balanced(
    api: Api, app_engine: AsyncEngine, steps: list[tuple[str, int]]
) -> None:
    async with shop(api) as s:
        pid, provider_id, intent_id, _row = await pending_payment(s, app_engine)
        service, ledger = s.service(), Ledger(api.factory)
        captured, refunded = False, 0
        for kind, value in steps:
            if kind == "capture":
                s.fake.capture(provider_id, fee=value)
                captured = True
            elif kind == "settle" and captured:
                s.fake.settle(provider_id)
            elif kind == "external_refund" and captured and value <= 1000 - refunded:
                s.fake.refund_externally(provider_id, Money(value, BRL))
                refunded += value
            elif kind == "refund":
                accepted = await s.refund(pid, amount=value)
                expected = captured and value <= 1000 - refunded
                assert (accepted.status_code == 201) is expected, accepted.text
                refunded += value if expected else 0
            await service.refresh(intent_id)
            await ledger.assert_consistent(s.project_uuid)
        view = (await s.get(f"/{pid}")).json()
        found = await balances(app_engine, s.project_uuid)

    assert view["refunded_amount"] == refunded <= (1000 if captured else 0)
    assert found.get("platform_revenue", (0, 0)) == ((refunded - 1000 if captured else 0), 0)
