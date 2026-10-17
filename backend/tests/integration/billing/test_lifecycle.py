"""Invoice lifecycle (W5.3, FINANCIAL_MODEL R1/R11): close -> finalize -> paid | void.

Real Postgres as ``fh_app`` and the scriptable fake provider. The clock is pinned (``T0`` =
2026-03-01) and periods are closed through the service, as the ``billing.close_due`` schedule
does. Every test ends with the ledger consistent (trial balance zero, cached balances exact).
"""

from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from financial_hub.modules.billing.application.api import (
    Invoice,
    InvoiceStatus,
    SubscriptionStatus,
    SubscriptionView,
)
from financial_hub.modules.ledger.domain.recipes import invoice_finalized_key
from financial_hub.shared.errors import Conflict
from tests.integration.billing.helpers import (
    CALLS_PRICE,
    FEE,
    FEE_PRICE,
    METRIC,
    T0,
    USAGE_ONLY_PLAN,
    Billing,
    billing,
    intents_of,
    journal,
    receivable,
    utc,
)
from tests.integration.identity.support import Api
from tests.integration.ledger.support import Ledger


async def closed_march(b: Billing, calls: int = 1500) -> tuple[SubscriptionView, Invoice]:
    """A subscription from T0 with ``calls`` used in March, closed on April 1st."""
    subscription = await b.subscribe()
    b.api.clock.set(utc(3, 20))
    await b.use(calls - 500, utc(3, 10))
    await b.use(500, utc(3, 15))
    created = await b.close(subscription, utc(4, 1, 1))
    assert len(created) == 1
    return subscription, await b.invoice(created[0])


async def test_close_finalizes_one_invoice_once_with_r1_and_one_payment(
    api: Api, app_engine: AsyncEngine
) -> None:
    async with billing(api) as b:
        subscription, invoice = await closed_march(b)

        # 4990 fee + 1500 calls x 0.05 = 7500 -> 12490 minor units, nothing credited
        assert invoice.status is InvoiceStatus.FINALIZED
        assert (invoice.period_start, invoice.period_end) == (T0, utc(4, 1))
        assert (invoice.total, invoice.credits_applied, invoice.amount_due) == (12490, 0, 12490)
        fee, calls = invoice.lines
        assert (fee.price_code, fee.amount, fee.quantity, fee.price_version) == (
            FEE_PRICE,
            FEE,
            None,
            1,
        )
        assert (calls.price_code, calls.metric_code, calls.quantity, calls.amount) == (
            CALLS_PRICE,
            METRIC,
            Decimal(1500),
            7500,
        )
        assert {fee.rounding, calls.rounding} == {"line:ROUND_HALF_EVEN"}
        assert invoice.payment_intent_id is not None
        assert invoice.due_date is not None

        # the close is idempotent: the same period is never invoiced twice
        assert await b.close(subscription, utc(4, 1, 2)) == []
        assert (
            await b.service().close_subscription(b.project_id, subscription.subscription.id) == []
        )
        assert len(await b.invoices(subscription)) == 1

        entries = await journal(app_engine, b.project_id)
        assert entries == [("R1", invoice_finalized_key(invoice.id))]
        assert await intents_of(app_engine, invoice.id) == [(12490, "created")]
        assert await receivable(app_engine, b.project_id) == 12490

        renewed = await b.service().get_subscription(b.project_id, subscription.subscription.id)
        assert renewed.subscription.status is SubscriptionStatus.ACTIVE
        assert (
            renewed.subscription.current_period_start,
            renewed.subscription.current_period_end,
        ) == (utc(4, 1), utc(5, 1))
        await Ledger(api.factory).assert_consistent(b.project_id)


async def test_capture_pays_the_invoice_with_r2_r3_and_no_second_r1(
    api: Api, app_engine: AsyncEngine
) -> None:
    async with billing(api) as b:
        _, invoice = await closed_march(b)
        api.clock.set(utc(4, 3))

        paid = await b.capture(invoice)

        assert paid is not None
        assert paid.status is InvoiceStatus.PAID
        assert paid.paid_at == utc(4, 3)
        recipes = sorted(recipe for recipe, _ in await journal(app_engine, b.project_id))
        assert recipes == ["R1", "R2", "R3"]
        assert await receivable(app_engine, b.project_id) == 0
        # the handler runs on every payment status change: marking paid again is a no-op
        assert invoice.payment_intent_id is not None
        assert await b.mark_paid(invoice.payment_intent_id) is None
        assert (await b.invoice(invoice.id)).status is InvoiceStatus.PAID
        await Ledger(api.factory).assert_consistent(b.project_id)


async def test_zero_amount_invoice_is_paid_at_once_without_a_payment(
    api: Api, app_engine: AsyncEngine
) -> None:
    async with billing(api) as b:
        subscription = await b.subscribe(USAGE_ONLY_PLAN)
        (invoice_id,) = await b.close(subscription, utc(4, 1, 1))

        invoice = await b.invoice(invoice_id)
        assert invoice.status is InvoiceStatus.PAID
        assert (invoice.total, invoice.amount_due) == (0, 0)
        assert invoice.payment_intent_id is None
        assert invoice.lines[0].quantity == Decimal(0)
        assert await intents_of(app_engine, invoice.id) == []
        assert await journal(app_engine, b.project_id) == []
        await Ledger(api.factory).assert_consistent(b.project_id)


async def test_void_reverses_r1_and_cancels_the_payment(api: Api, app_engine: AsyncEngine) -> None:
    async with billing(api) as b:
        _, invoice = await closed_march(b)

        voided = await b.service().void_invoice(b.scope(), invoice.id)

        assert voided.status is InvoiceStatus.VOID
        assert voided.voided_at is not None
        recipes = sorted(recipe for recipe, _ in await journal(app_engine, b.project_id))
        assert recipes == ["R1", "R11"]
        assert await receivable(app_engine, b.project_id) == 0
        ((_, status),) = await intents_of(app_engine, invoice.id)
        assert status == "created"  # the intent row stays; its provider payment is canceled
        with pytest.raises(Conflict):
            await b.service().void_invoice(b.scope(), invoice.id)
        with pytest.raises(Conflict):
            await b.service().finalize_invoice(b.scope(), invoice.id)
        # a capture racing the void is unapplied cash (R2b), never a paid void invoice
        assert invoice.payment_intent_id is not None
        assert await b.mark_paid(invoice.payment_intent_id) is None
        await Ledger(api.factory).assert_consistent(b.project_id)


async def test_a_paid_invoice_cannot_be_voided(api: Api) -> None:
    async with billing(api) as b:
        _, invoice = await closed_march(b)
        await b.capture(invoice)

        with pytest.raises(Conflict):
            await b.service().void_invoice(b.scope(), invoice.id)
        assert (await b.invoice(invoice.id)).status is InvoiceStatus.PAID


async def test_late_usage_lands_on_the_next_invoice(api: Api) -> None:
    async with billing(api) as b:
        subscription, march = await closed_march(b, calls=1000)
        assert march.lines[1].quantity == Decimal(1000)

        api.clock.set(utc(4, 2))
        await b.use(40, utc(3, 28))  # recorded after March closed
        api.clock.set(utc(4, 20))
        await b.use(10, utc(4, 15))
        (april_id,) = await b.close(subscription, utc(5, 1, 1))

        april = await b.invoice(april_id)
        assert (april.period_start, april.period_end) == (utc(4, 1), utc(5, 1))
        usage = april.lines[1]
        assert usage.quantity == Decimal(50)
        assert Decimal(usage.details["usage"]["previously_billed"]) == 1000
        assert april.total == FEE + 250  # 50 calls x 0.05
        assert (await b.invoice(march.id)).total == FEE + 5000  # never re-rated
        await Ledger(api.factory).assert_consistent(b.project_id)


async def test_mid_period_start_prorates_the_fee_but_not_usage(api: Api) -> None:
    async with billing(api) as b:
        api.clock.set(utc(3, 11))
        subscription = await b.subscribe(anchor_day=1)
        assert subscription.subscription.current_period_end == utc(4, 1)
        api.clock.set(utc(3, 20))
        await b.use(100, utc(3, 12))
        (invoice_id,) = await b.close(subscription, utc(4, 1, 1))

        fee, calls = (await b.invoice(invoice_id)).lines
        # 4990 x 21 / 31 = 3380.32 -> 3380; 100 calls x 0.05 = 500 (usage is never prorated)
        assert fee.amount == 3380
        assert fee.details["proration"] == {"active_days": 21, "period_days": 31}
        assert calls.amount == 500


async def test_trial_period_is_not_invoiced(api: Api, app_engine: AsyncEngine) -> None:
    async with billing(api) as b:
        subscription = await b.subscribe(trial_days=14)
        assert subscription.subscription.status is SubscriptionStatus.TRIALING
        assert subscription.subscription.current_period_end == utc(3, 15)

        assert await b.close(subscription, utc(3, 15, 1)) == []

        active = await b.service().get_subscription(b.project_id, subscription.subscription.id)
        assert active.subscription.status is SubscriptionStatus.ACTIVE
        assert active.subscription.anchor_day == 15
        assert active.subscription.current_period_end == utc(4, 15)
        assert await journal(app_engine, b.project_id) == []


async def test_close_catches_up_every_ended_period_oldest_first(api: Api) -> None:
    async with billing(api) as b:
        subscription = await b.subscribe()

        created = await b.close(subscription, utc(6, 1, 1))

        assert len(created) == 3
        invoices = await b.invoices(subscription)
        assert [invoice.period_start for invoice in invoices] == [T0, utc(4, 1), utc(5, 1)]
        assert all(invoice.total == FEE for invoice in invoices)
        await Ledger(api.factory).assert_consistent(b.project_id)


async def test_cancel_at_period_end_bills_the_last_period_then_stops(api: Api) -> None:
    async with billing(api) as b:
        subscription = await b.subscribe()
        await b.service().cancel_subscription(
            b.scope(), subscription.subscription.id, at_period_end=True
        )

        assert len(await b.close(subscription, utc(5, 1, 1))) == 1
        ended = await b.service().get_subscription(b.project_id, subscription.subscription.id)
        assert ended.subscription.status is SubscriptionStatus.CANCELED
        assert ended.subscription.ended_at == utc(4, 1)


async def test_cancel_now_bills_the_partial_period_prorated(api: Api) -> None:
    async with billing(api) as b:
        subscription = await b.subscribe()
        api.clock.set(utc(3, 10))
        await b.use(100, utc(3, 5))
        api.clock.set(utc(3, 11))

        await b.service().cancel_subscription(
            b.scope(), subscription.subscription.id, at_period_end=False
        )

        (final,) = await b.invoices(subscription)
        assert final.status is InvoiceStatus.FINALIZED
        assert (final.period_start, final.period_end) == (T0, utc(3, 11))
        fee, calls = final.lines
        # 4990 x 10 / 31 = 1609.68 -> 1610; usage to date is never prorated
        assert fee.details["proration"] == {"active_days": 10, "period_days": 31}
        assert (fee.amount, calls.amount) == (1610, 500)
        ended = await b.service().get_subscription(b.project_id, subscription.subscription.id)
        assert ended.subscription.status is SubscriptionStatus.CANCELED
        assert await b.close(subscription, utc(4, 1, 1)) == []  # nothing left to bill
        await Ledger(api.factory).assert_consistent(b.project_id)


async def test_scheduler_close_due_picks_up_ended_periods(api: Api) -> None:
    async with billing(api) as b:
        subscription = await b.subscribe()
        api.clock.set(utc(4, 1, 1))

        assert await b.service().close_due(limit=500) >= 1

        assert len(await b.invoices(subscription)) == 1
