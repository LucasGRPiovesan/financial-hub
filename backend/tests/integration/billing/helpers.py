"""Builders for the billing suites: a shop (fake provider) with a metric, two prices and a plan.

The clock is pinned to ``T0`` (2026-03-01 00:00 UTC) so periods are whole calendar months; tests
move it with ``api.clock.set`` and close periods through the service, as the scheduler does.
"""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import count
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from financial_hub.modules.billing.application.api import (
    BillingService,
    Invoice,
    SubscriptionView,
)
from financial_hub.modules.identity.application.service import OPERATOR_PRINCIPAL
from financial_hub.modules.metering.application.service import MeteringService, UsageItem
from financial_hub.modules.pricing.application.api import PriceCatalogService
from financial_hub.platform.db import UnitOfWork
from financial_hub.platform.http.auth import ProjectScope
from financial_hub.shared.pagination import PageParams
from tests.integration.identity.support import Api
from tests.integration.payments.helpers import Shop, shop

T0 = datetime(2026, 3, 1, tzinfo=UTC)
PRICES_FROM = datetime(2026, 1, 1, tzinfo=UTC)
METRIC = "api_calls"
FEE_PRICE = "monthly-fee"
CALLS_PRICE = "api-calls"
PLAN = "pro"
USAGE_ONLY_PLAN = "usage-only"
FEE = 4990  # BRL 49.90 a month
UNIT_PRICE = "0.05"  # BRL 0.05 a call
BILLING_SCOPES = ("payments:write", "billing:read", "billing:write")

_sequence = count(1)


def utc(month: int, day: int, hour: int = 0) -> datetime:
    return datetime(2026, month, day, hour, tzinfo=UTC)


@dataclass
class Billing:
    shop: Shop

    @property
    def api(self) -> Api:
        return self.shop.api

    @property
    def project_id(self) -> UUID:
        return self.shop.project_uuid

    @property
    def base(self) -> str:
        return f"/v1/projects/{self.shop.project_id}"

    def scope(self) -> ProjectScope:
        """The owner, acting from the console (catalog and invoice changes are operator-only)."""
        return ProjectScope(
            project_id=self.project_id,
            principal_kind=OPERATOR_PRINCIPAL,
            principal_id=self.shop.owner.id,
            permissions=frozenset(),
        )

    def service(self) -> BillingService:
        return BillingService(self.api.factory, clock=self.api.clock, payments=self.shop.service())

    async def catalog(self) -> None:
        """Metric ``api_calls`` (sum of ``calls``), a monthly fee, a per-call price, two plans."""
        async with UnitOfWork(self.api.factory) as uow:
            await MeteringService(uow, clock=self.api.clock).create_metric(
                self.scope(),
                code=METRIC,
                name="API calls",
                description=None,
                aggregation="sum",
                field="calls",
            )
            prices = PriceCatalogService(uow, clock=self.api.clock)
            await prices.create_price(
                self.scope(),
                code=FEE_PRICE,
                name="Monthly fee",
                currency="BRL",
                model={"kind": "flat_fee", "amount": FEE},
                effective_from=PRICES_FROM,
            )
            await prices.create_price(
                self.scope(),
                code=CALLS_PRICE,
                name="API calls",
                currency="BRL",
                model={"kind": "per_unit", "unit_price": UNIT_PRICE},
                metric_code=METRIC,
                effective_from=PRICES_FROM,
            )
        service = self.service()
        await service.create_plan(
            self.scope(), code=PLAN, name="Pro", price_codes=[FEE_PRICE, CALLS_PRICE]
        )
        await service.create_plan(
            self.scope(), code=USAGE_ONLY_PLAN, name="Usage only", price_codes=[CALLS_PRICE]
        )

    async def subscribe(self, plan: str = PLAN, **options: Any) -> SubscriptionView:
        return await self.service().create_subscription(
            self.scope(), customer_id=self.shop.customer_id, plan_code=plan, **options
        )

    async def use(self, calls: int, at: datetime) -> None:
        """Record ``calls`` API calls at ``at`` (within the ingest window of the clock)."""
        async with UnitOfWork(self.api.factory) as uow:
            await MeteringService(uow, clock=self.api.clock).ingest(
                self.project_id,
                UsageItem(
                    transaction_id=f"call-{next(_sequence)}-{id(self)}",
                    metric=METRIC,
                    properties={"calls": calls},
                    timestamp=at,
                    customer_external_ref="buyer-1",
                ),
            )

    async def close(self, subscription: SubscriptionView, at: datetime) -> list[UUID]:
        self.api.clock.set(at)
        return await self.service().close_subscription(
            self.project_id, subscription.subscription.id
        )

    async def invoices(self, subscription: SubscriptionView) -> list[Invoice]:
        """The subscription's invoices, oldest period first."""
        page = await self.service().list_invoices(
            self.project_id, PageParams(limit=100), subscription_id=subscription.subscription.id
        )
        return sorted(page.data, key=lambda invoice: invoice.period_start)

    async def invoice(self, invoice_id: UUID) -> Invoice:
        return await self.service().get_invoice(self.project_id, invoice_id)

    async def capture(self, invoice: Invoice) -> Invoice | None:
        """The provider captures the invoice's payment; the webhook path, then the paid handler."""
        assert invoice.payment_intent_id is not None
        async with self.api.factory() as session:
            provider_id = (
                await session.execute(
                    text(
                        "SELECT provider_payment_id FROM provider_payments "
                        "WHERE payment_intent_id = :intent"
                    ),
                    {"intent": invoice.payment_intent_id},
                )
            ).scalar_one()
        self.shop.fake.capture(provider_id)
        await self.shop.service().refresh(invoice.payment_intent_id)
        return await self.mark_paid(invoice.payment_intent_id)

    async def mark_paid(self, intent_id: UUID) -> Invoice | None:
        async with UnitOfWork(self.api.factory) as uow:
            return await self.service().mark_paid(uow, intent_id)


@asynccontextmanager
async def billing(api: Api, *, scopes: Sequence[str] = BILLING_SCOPES) -> AsyncIterator[Billing]:
    api.clock.set(T0)
    async with shop(api, scopes=tuple(scopes)) as running:
        built = Billing(running)
        await built.catalog()
        yield built


async def journal(engine: AsyncEngine, project: UUID) -> list[tuple[str, str]]:
    """``(recipe, idempotency key)`` of every entry of the project, oldest first."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT recipe, idempotency_key FROM journal_entries "
                "WHERE project_id = :project ORDER BY created_at, id"
            ),
            {"project": project},
        )
        return [(row.recipe, row.idempotency_key) for row in rows.all()]


async def intents_of(engine: AsyncEngine, invoice_id: UUID) -> list[tuple[int, str]]:
    """``(amount, creation status)`` of the payment intents collecting the invoice."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT amount, creation_status FROM payment_intents WHERE invoice_id = :invoice"),
            {"invoice": invoice_id},
        )
        return [(row.amount, row.creation_status) for row in rows.all()]


async def receivable(engine: AsyncEngine, project: UUID) -> int:
    """Posted ``Dr - Cr`` of every customer receivable of the project."""
    async with engine.connect() as conn:
        value = await conn.execute(
            text(
                "SELECT coalesce(sum(posted_dr - posted_cr), 0) FROM ledger_accounts "
                "WHERE project_id = :project AND code LIKE 'customer_receivable%'"
            ),
            {"project": project},
        )
        return int(value.scalar_one())
