"""Reconciliation runs against the fake provider: clean runs, every break kind, auto-resolution."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from financial_hub.modules.ledger.application import (
    AccountSpec,
    AccountType,
    Direction,
    EntryDraft,
    PostingDraft,
)
from financial_hub.modules.reconciliation.application.api import Break
from financial_hub.shared.ids import IdPrefix, format_id
from financial_hub.shared.money import BRL, Money
from tests.integration.identity.support import Api
from tests.integration.ledger.support import Ledger
from tests.integration.payments.helpers import events_of, intent_uuid
from tests.integration.reconciliation.helpers import Desk, desk, run_id

SALES_CLEARING = AccountSpec("sales_clearing", AccountType.LIABILITY)
SUSPENSE = AccountSpec("test:suspense", AccountType.ASSET)


def kinds(found: list[Break]) -> list[str]:
    return sorted(item.kind.value for item in found)


def break_id(found: Break) -> str:
    return format_id(IdPrefix.RECONCILIATION_BREAK, found.id)


async def published(engine: AsyncEngine, d: Desk, subject: str, kind: str) -> list[Any]:
    return [
        data for type_, data in await events_of(engine, d.project_uuid, subject) if type_ == kind
    ]


async def captured(d: Desk, fee: int = 99) -> tuple[str, str, UUID]:
    """A captured (unsettled) hub payment: ``(public id, provider id, intent)``."""
    created = await d.shop.create()
    assert created.status_code == 201, created.text
    view = created.json()
    intent_id = intent_uuid(view["id"])
    d.shop.fake.capture(view["provider_payment_id"], fee=fee)
    await d.shop.service().refresh(intent_id)
    return view["id"], view["provider_payment_id"], intent_id


async def test_clean_runs_open_no_breaks(api: Api) -> None:
    async with desk(api) as d:
        await d.settled()
        await captured(d)
        d.next_day()
        runs = [await d.run(kind) for kind in ("transactions", "internal", "balance")]
        found = await d.breaks()
        await Ledger(api.factory).assert_consistent(d.project_uuid)

    assert [run.status.value for run in runs] == ["completed"] * 3
    assert [run.breaks_opened for run in runs] == [0, 0, 0]
    assert runs[0].summary["matched"] == 2
    assert runs[2].summary["provider_minor"] == runs[2].summary["booked_minor"] == 901
    assert found == []


async def test_a_run_is_idempotent_by_window(api: Api, app_engine: AsyncEngine) -> None:
    async with desk(api) as d:
        _pid, provider_id, _intent = await d.settled()
        d.shop.fake.override(provider_id, amount=Money(1500, BRL))
        d.next_day()
        first = await d.run("transactions")
        again = await d.run("transactions")
        found = await d.breaks()
        completed = await published(app_engine, d, run_id(first), "reconciliation.run_completed")
        opened = await published(app_engine, d, break_id(found[0]), "reconciliation.break_opened")

    assert again.id == first.id
    assert first.breaks_opened == 1
    assert kinds(found) == ["amount_mismatch"]
    assert [event["status"] for event in completed] == ["completed"]
    assert [event["kind"] for event in opened] == ["amount_mismatch"]


async def test_amount_mismatch_keeps_both_sides_in_minor_units(api: Api) -> None:
    async with desk(api) as d:
        _pid, provider_id, intent_id = await d.settled()
        d.shop.fake.override(provider_id, amount=Money(1500, BRL))
        d.next_day()
        run = await d.run("transactions")
        [found] = await d.breaks(run)

    assert found.kind.value == "amount_mismatch"
    assert found.state.value == "open"
    assert found.difference == 500
    assert found.payment_intent_id == intent_id
    assert found.provider_payment_id == provider_id
    assert found.internal is not None and found.internal["amount_minor"] == 1000
    assert found.external is not None and found.external["amount_minor"] == 1500
    assert str(found.subject["provider_account_id"]).startswith("pva_")  # the R12 target


async def test_a_revised_fee_is_auto_resolved_by_a_refresh(
    api: Api, app_engine: AsyncEngine
) -> None:
    async with desk(api) as d:
        _pid, provider_id, _intent = await captured(d, fee=99)
        d.shop.fake.capture(provider_id, fee=120)  # the provider now reports net 880
        d.next_day()
        run = await d.run("transactions")
        [found] = await d.breaks(run)
        resolved = await published(app_engine, d, break_id(found), "reconciliation.break_resolved")
        await Ledger(api.factory).assert_consistent(d.project_uuid)

    assert found.kind.value == "fee_mismatch"
    assert found.difference == -21
    assert found.state.value == "resolved"
    assert found.resolved_by == "system"
    assert [(event["state"], event["automatic"]) for event in resolved] == [("resolved", True)]


async def test_a_missed_webhook_is_auto_resolved(api: Api) -> None:
    async with desk(api) as d:
        created = await d.shop.create()
        provider_id = created.json()["provider_payment_id"]
        d.shop.fake.capture(provider_id, fee=99)  # the hub never heard of it
        d.next_day()
        run = await d.run("transactions")
        [found] = await d.breaks(run)
        view = await d.shop.get(f"/{created.json()['id']}")

    assert found.kind.value == "status_mismatch"
    assert found.state.value == "resolved"
    assert found.internal is not None and found.internal["capture_status"] == "pending"
    assert view.json()["status"] == "succeeded"


async def test_a_payment_the_hub_does_not_know_is_missing_internal(api: Api) -> None:
    async with desk(api) as d:
        await d.settled()
        stray = d.shop.fake.add_external_payment(Money(700, BRL), fee=10)
        d.next_day()
        run = await d.run("transactions")
        [found] = await d.breaks(run)

    assert found.kind.value == "missing_internal"
    assert found.provider_payment_id == stray.provider_payment_id
    assert found.internal is None
    assert found.difference == 700
    assert found.state.value == "open"


async def test_a_payment_the_provider_lost_is_missing_external(api: Api) -> None:
    async with desk(api) as d:
        _pid, provider_id, intent_id = await d.settled()
        d.shop.fake.forget(provider_id)
        d.next_day()
        run = await d.run("transactions")
        [found] = await d.breaks(run)

    assert found.kind.value == "missing_external"
    assert found.payment_intent_id == intent_id
    assert found.external is None
    assert found.difference == -1000


async def test_a_provider_balance_off_the_books_is_balance_drift(api: Api) -> None:
    async with desk(api) as d:
        await d.settled()
        d.shop.fake.balance_override = Money(1001, BRL)
        d.next_day()
        run = await d.run("balance")
        [found] = await d.breaks(run)
        again = await d.run("balance")

    assert found.kind.value == "balance_drift"
    assert found.difference == 100
    assert found.external == {"balance_minor": 1001}
    assert found.internal is not None and found.internal["total_minor"] == 901
    assert found.state.value == "open"  # never auto-adjusted
    assert again.id == run.id


async def test_materialized_balances_off_their_postings_are_ledger_drift(
    api: Api, owner_engine: AsyncEngine
) -> None:
    async with desk(api) as d:
        await d.settled()
        async with owner_engine.begin() as conn:
            await conn.execute(text("SET LOCAL fh.ledger_apply = 'on'"))
            account = (
                await conn.execute(
                    text(
                        "UPDATE ledger_accounts SET posted_dr = posted_dr + 5 "
                        "WHERE project_id = :p AND code = 'platform_revenue' RETURNING id"
                    ),
                    {"p": d.project_uuid},
                )
            ).scalar_one()
        d.next_day()
        run = await d.run("internal")
        found = await d.breaks(run)

    assert kinds(found) == ["ledger_drift", "ledger_drift"]  # the account and the trial balance
    by_account = next(item for item in found if item.account_id is not None)
    assert by_account.account_id == account
    assert by_account.difference == -5
    trial = next(item for item in found if item.account_id is None)
    assert trial.subject == {"scope": "trial_balance", "currency": "BRL"}


async def test_a_clearing_residue_of_a_settled_payment_is_clearing_not_zero(api: Api) -> None:
    async with desk(api) as d:
        _pid, _provider_id, intent_id = await d.settled()
        draft = EntryDraft.create(
            project_id=d.project_uuid,
            idempotency_key=f"test:residue:{intent_id}",
            recipe="test",
            postings=[
                PostingDraft(SALES_CLEARING, Direction.DEBIT, Money(50, BRL)),
                PostingDraft(SUSPENSE, Direction.CREDIT, Money(50, BRL)),
            ],
            effective_at=datetime(2026, 10, 1, tzinfo=UTC),
            correlation_id=intent_id,
        )
        assert draft is not None
        await Ledger(api.factory).post(draft)
        d.next_day()
        run = await d.run("internal")
        [found] = await d.breaks(run)

    assert found.kind.value == "clearing_not_zero"
    assert found.payment_intent_id == intent_id
    assert found.subject["code"] == "sales_clearing"
    assert found.difference == 50


async def test_an_unreachable_provider_fails_the_run(api: Api, app_engine: AsyncEngine) -> None:
    async with desk(api) as d:
        await d.settled()
        d.shop.fake.listing_unavailable = True
        d.next_day()
        run = await d.run("transactions")
        completed = await published(app_engine, d, run_id(run), "reconciliation.run_completed")

    assert run.status.value == "failed"
    assert run.error_code == "provider_unavailable"
    assert [event["status"] for event in completed] == ["failed"]
