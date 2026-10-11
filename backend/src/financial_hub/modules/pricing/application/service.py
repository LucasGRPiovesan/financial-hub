"""Pricing use cases over the ``PriceBookRepository`` port. No I/O of its own."""

from datetime import datetime
from uuid import UUID

from financial_hub.modules.pricing.application.ports import PriceBookRepository
from financial_hub.modules.pricing.domain import (
    BillingPeriod,
    Proration,
    RatedCharge,
    Usage,
    rate,
)
from financial_hub.shared.errors import NotFound


class PricingService:
    def __init__(self, repository: PriceBookRepository) -> None:
        self._repository = repository

    async def rate(
        self,
        project_id: UUID,
        price_code: str,
        usage: Usage,
        period: BillingPeriod,
        *,
        at: datetime | None = None,
        proration: Proration | None = None,
    ) -> RatedCharge:
        """Rate ``usage`` with the version of ``price_code`` effective at ``at``.

        ``at`` defaults to the period start, so a whole period is rated with one version.
        """
        price = await self._repository.get_price_by_code(project_id, price_code)
        if price is None:
            raise NotFound(f"no price {price_code!r} in this project")
        version = price.version_at(period.start if at is None else at)
        return rate(version, usage, period, proration=proration)
