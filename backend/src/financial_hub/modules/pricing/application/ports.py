"""Persistence port for price books. Implemented by ``infrastructure`` in task 5.2+."""

from typing import Protocol
from uuid import UUID

from financial_hub.modules.pricing.domain import Price, PriceBook, PriceVersion


class PriceBookRepository(Protocol):
    """Stores prices and their versions. Versions are append-only: implementations never update
    a version row except to set ``effective_to`` of the open-ended one (``close_version``), and
    must enforce non-overlapping windows per price in the database too (ADR-0007)."""

    async def get_book(self, project_id: UUID) -> PriceBook:
        """Every price of the project with all its versions (empty book if none)."""
        ...

    async def get_price(self, project_id: UUID, price_id: UUID) -> Price | None: ...

    async def get_price_by_code(self, project_id: UUID, code: str) -> Price | None: ...

    async def add_price(self, price: Price) -> None:
        """Insert a new price and the versions it already carries."""
        ...

    async def add_version(self, price: Price, version: PriceVersion) -> None:
        """Append ``version`` (``price.latest``) to an existing price."""
        ...

    async def close_version(self, version: PriceVersion) -> None:
        """Persist the ``effective_to`` set by ``Price.close_open_version``."""
        ...
