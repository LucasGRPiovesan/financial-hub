"""Public API of the pricing module (ARCHITECTURE s3): other modules import only from here.

Billing reads the catalog through :func:`catalog_entries` / :func:`catalog_entries_by_code` (a
price, its versions and the metric it rates) and rates with :class:`PricingService` over
:func:`price_book`, all on the session of its own unit of work.
"""

from collections.abc import Iterable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from financial_hub.modules.pricing.application.catalog import (
    PriceCatalogService,
    PriceVersionView,
    PriceView,
    operator_only,
    requires_metric,
)
from financial_hub.modules.pricing.application.ports import PriceBookRepository
from financial_hub.modules.pricing.application.service import PricingService
from financial_hub.modules.pricing.domain import (
    ROUNDING_POLICY,
    AggregatedUsage,
    BaseFeeWithOverage,
    BillingPeriod,
    ChargeLine,
    FlatFee,
    Graduated,
    MinimumCommitment,
    Package,
    PackageRounding,
    Percentage,
    PerUnit,
    Price,
    PriceBook,
    PriceModel,
    PriceVersion,
    Proration,
    RatedCharge,
    Tier,
    Usage,
    Volume,
    parse_quantity,
    parse_unit_price,
    price_lines,
    rate,
)
from financial_hub.modules.pricing.infrastructure import repository
from financial_hub.modules.pricing.infrastructure.codec import model_from_json, model_to_json
from financial_hub.modules.pricing.infrastructure.repository import (
    CatalogEntry,
    SqlPriceBookRepository,
)
from financial_hub.shared.clock import Clock

__all__ = [
    "ROUNDING_POLICY",
    "AggregatedUsage",
    "BaseFeeWithOverage",
    "BillingPeriod",
    "CatalogEntry",
    "ChargeLine",
    "FlatFee",
    "Graduated",
    "MinimumCommitment",
    "Package",
    "PackageRounding",
    "PerUnit",
    "Percentage",
    "Price",
    "PriceBook",
    "PriceBookRepository",
    "PriceCatalogService",
    "PriceModel",
    "PriceVersion",
    "PriceVersionView",
    "PriceView",
    "PricingService",
    "Proration",
    "RatedCharge",
    "Tier",
    "Usage",
    "Volume",
    "catalog_entries",
    "catalog_entries_by_code",
    "model_from_json",
    "model_to_json",
    "operator_only",
    "parse_quantity",
    "parse_unit_price",
    "price_book",
    "price_lines",
    "rate",
    "requires_metric",
]


def price_book(session: AsyncSession, *, clock: Clock) -> PriceBookRepository:
    """The price book of the caller's unit of work (``PricingService(price_book(...))``)."""
    return SqlPriceBookRepository(session, clock=clock)


async def catalog_entries(
    session: AsyncSession, project_id: UUID, price_ids: Iterable[UUID]
) -> list[CatalogEntry]:
    """The project's prices among ``price_ids`` (unknown ids are left out)."""
    return await repository.get_entries(session, project_id, price_ids)


async def catalog_entries_by_code(
    session: AsyncSession, project_id: UUID, codes: Iterable[str]
) -> list[CatalogEntry]:
    """The project's prices among ``codes`` (unknown codes are left out)."""
    return await repository.get_entries_by_code(session, project_id, codes)
