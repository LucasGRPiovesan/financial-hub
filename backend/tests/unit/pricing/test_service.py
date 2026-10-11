from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from financial_hub.modules.pricing.application.api import (
    BillingPeriod,
    PerUnit,
    Price,
    PriceBook,
    PriceBookRepository,
    PriceVersion,
    PricingService,
)
from financial_hub.shared.errors import NotFound
from financial_hub.shared.money import BRL, Money

JAN = datetime(2026, 1, 1, tzinfo=UTC)
FEB = datetime(2026, 2, 1, tzinfo=UTC)
MAR = datetime(2026, 3, 1, tzinfo=UTC)


class InMemoryPriceBooks:
    """Test double proving the ``PriceBookRepository`` protocol is implementable."""

    def __init__(self) -> None:
        self.prices: dict[UUID, Price] = {}

    async def get_book(self, project_id: UUID) -> PriceBook:
        return PriceBook(
            project_id, tuple(p for p in self.prices.values() if p.project_id == project_id)
        )

    async def get_price(self, project_id: UUID, price_id: UUID) -> Price | None:
        price = self.prices.get(price_id)
        return price if price is not None and price.project_id == project_id else None

    async def get_price_by_code(self, project_id: UUID, code: str) -> Price | None:
        candidates = self.prices.values()
        return next((p for p in candidates if p.project_id == project_id and p.code == code), None)

    async def add_price(self, price: Price) -> None:
        self.prices[price.id] = price

    async def add_version(self, price: Price, version: PriceVersion) -> None:
        self.prices[price.id] = price

    async def close_version(self, version: PriceVersion) -> None:
        price = self.prices[version.price_id]
        versions = tuple(version if v.id == version.id else v for v in price.versions)
        self.prices[price.id] = Price(price.id, price.project_id, price.code, BRL, versions)


async def test_service_rates_with_the_version_effective_at_period_start() -> None:
    repository = InMemoryPriceBooks()
    port: PriceBookRepository = repository
    project_id = uuid4()
    price = (
        Price(uuid4(), project_id, "api_calls", BRL)
        .add_version(uuid4(), PerUnit(BRL, Decimal("0.10")), JAN, FEB)
        .add_version(uuid4(), PerUnit(BRL, Decimal("0.20")), FEB)
    )
    await port.add_price(price)
    service = PricingService(port)

    january = BillingPeriod(JAN, FEB)
    february = BillingPeriod(FEB, MAR)
    assert (await service.rate(project_id, "api_calls", 10, january)).total == Money(100, BRL)
    charge = await service.rate(project_id, "api_calls", 10, february)
    assert (charge.total, charge.price_version) == (Money(200, BRL), 2)
    # an explicit instant overrides the period start
    charge = await service.rate(project_id, "api_calls", 10, january, at=FEB - (FEB - JAN) / 2)
    assert charge.price_version == 1

    with pytest.raises(NotFound):
        await service.rate(project_id, "missing", 10, january)
    with pytest.raises(NotFound):
        await service.rate(uuid4(), "api_calls", 10, january)

    assert (await port.get_book(project_id)).price("api_calls") == price
    assert await port.get_price(project_id, price.id) == price
    assert await port.get_price(uuid4(), price.id) is None
