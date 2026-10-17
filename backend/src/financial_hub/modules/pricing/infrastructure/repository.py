"""Price catalog persistence on the caller's session (runs as ``fh_app``).

``prices`` rows are immutable and ``price_versions`` append-only: the only UPDATE is setting the
``effective_to`` of the open-ended version (a trigger refuses anything else) and
``ex_price_versions_no_overlap`` keeps the windows of one price disjoint in the database too.
Every read is scoped by ``project_id``.
"""

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from financial_hub.modules.pricing.domain import Price, PriceBook, PriceVersion
from financial_hub.modules.pricing.infrastructure.codec import model_from_json, model_to_json
from financial_hub.modules.pricing.infrastructure.tables import price_versions, prices
from financial_hub.shared.clock import Clock
from financial_hub.shared.money import Currency
from financial_hub.shared.pagination import Cursor, keyset_predicate

_price = prices
_version = price_versions


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """A price with its catalog attributes: ``metric_code`` is the metric a usage price rates."""

    price: Price
    name: str
    metric_code: str | None
    created_at: datetime


async def _versions(
    session: AsyncSession, project_id: UUID, rows: Sequence[Any]
) -> dict[UUID, list[PriceVersion]]:
    found: dict[UUID, list[PriceVersion]] = defaultdict(list)
    if not rows:
        return found
    currencies = {row.id: Currency.of(row.currency) for row in rows}
    statement = (
        select(_version)
        .where(_version.c.project_id == project_id, _version.c.price_id.in_(list(currencies)))
        .order_by(_version.c.price_id, _version.c.version)
    )
    for row in (await session.execute(statement)).all():
        found[row.price_id].append(
            PriceVersion(
                id=row.id,
                price_id=row.price_id,
                version=row.version,
                model=model_from_json(row.model, currencies[row.price_id]),
                effective_from=row.effective_from,
                effective_to=row.effective_to,
            )
        )
    return found


async def _entries(
    session: AsyncSession, project_id: UUID, rows: Sequence[Any]
) -> list[CatalogEntry]:
    versions = await _versions(session, project_id, rows)
    return [
        CatalogEntry(
            price=Price(
                id=row.id,
                project_id=row.project_id,
                code=row.code,
                currency=Currency.of(row.currency),
                versions=tuple(versions[row.id]),
            ),
            name=row.name,
            metric_code=row.metric_code,
            created_at=row.created_at,
        )
        for row in rows
    ]


async def insert_price(session: AsyncSession, entry: CatalogEntry) -> bool:
    """Insert the price and its versions; ``False`` when the project already has the code."""
    price = entry.price
    statement = (
        insert(_price)
        .values(
            id=price.id,
            project_id=price.project_id,
            code=price.code,
            name=entry.name,
            currency=price.currency.code,
            metric_code=entry.metric_code,
            created_at=entry.created_at,
        )
        .on_conflict_do_nothing(index_elements=[_price.c.project_id, _price.c.code])
        .returning(_price.c.id)
    )
    if (await session.execute(statement)).first() is None:
        return False
    for version in price.versions:
        await insert_version(session, price.project_id, version, entry.created_at)
    return True


async def insert_version(
    session: AsyncSession, project_id: UUID, version: PriceVersion, now: datetime
) -> None:
    await session.execute(
        insert(_version).values(
            id=version.id,
            price_id=version.price_id,
            project_id=project_id,
            version=version.version,
            model=model_to_json(version.model),
            effective_from=version.effective_from,
            effective_to=version.effective_to,
            created_at=now,
        )
    )


async def close_version(session: AsyncSession, project_id: UUID, version: PriceVersion) -> bool:
    """Set ``effective_to`` of an open-ended version; ``False`` if it was already closed."""
    statement = (
        update(_version)
        .where(
            _version.c.id == version.id,
            _version.c.project_id == project_id,
            _version.c.effective_to.is_(None),
        )
        .values(effective_to=version.effective_to)
        .returning(_version.c.id)
    )
    return (await session.execute(statement)).first() is not None


async def get_entry(
    session: AsyncSession, project_id: UUID, code: str, *, lock: bool = False
) -> CatalogEntry | None:
    """The price ``code``; ``lock`` serializes writers of its versions (``FOR UPDATE``).

    The lock is taken on the version rows, before they are read: ``prices`` is append-only, so
    ``fh_app`` holds no UPDATE on it and may not lock its rows (migration 0013).
    """
    statement = select(_price).where(_price.c.project_id == project_id, _price.c.code == code)
    if lock:
        price_id = (
            select(_price.c.id)
            .where(_price.c.project_id == project_id, _price.c.code == code)
            .scalar_subquery()
        )
        await session.execute(
            select(_version.c.id)
            .where(_version.c.project_id == project_id, _version.c.price_id == price_id)
            .with_for_update()
        )
    rows = (await session.execute(statement)).all()
    found = await _entries(session, project_id, rows)
    return found[0] if found else None


async def get_entries(
    session: AsyncSession, project_id: UUID, price_ids: Iterable[UUID]
) -> list[CatalogEntry]:
    wanted = list(price_ids)
    if not wanted:
        return []
    statement = select(_price).where(_price.c.project_id == project_id, _price.c.id.in_(wanted))
    return await _entries(session, project_id, (await session.execute(statement)).all())


async def get_entries_by_code(
    session: AsyncSession, project_id: UUID, codes: Iterable[str]
) -> list[CatalogEntry]:
    wanted = list(codes)
    if not wanted:
        return []
    statement = select(_price).where(_price.c.project_id == project_id, _price.c.code.in_(wanted))
    return await _entries(session, project_id, (await session.execute(statement)).all())


async def list_entries(
    session: AsyncSession, project_id: UUID, *, cursor: Cursor | None, limit: int
) -> list[CatalogEntry]:
    """Newest first, ``limit`` rows at most (callers pass ``limit + 1`` to detect more)."""
    statement = select(_price).where(_price.c.project_id == project_id)
    if cursor is not None:
        statement = statement.where(keyset_predicate(_price.c.created_at, _price.c.id, cursor))
    statement = statement.order_by(_price.c.created_at.desc(), _price.c.id.desc()).limit(limit)
    return await _entries(session, project_id, (await session.execute(statement)).all())


class SqlPriceBookRepository:
    """The ``PriceBookRepository`` port over Postgres (the session of the caller's unit of work).

    ``add_price`` stores a price without catalog attributes (name = code, no metric); the catalog
    service writes through :func:`insert_price` instead.
    """

    def __init__(self, session: AsyncSession, *, clock: Clock) -> None:
        self._session = session
        self._clock = clock

    async def get_book(self, project_id: UUID) -> PriceBook:
        statement = select(_price).where(_price.c.project_id == project_id)
        rows = (await self._session.execute(statement)).all()
        entries = await _entries(self._session, project_id, rows)
        return PriceBook(project_id, tuple(entry.price for entry in entries))

    async def get_price(self, project_id: UUID, price_id: UUID) -> Price | None:
        found = await get_entries(self._session, project_id, [price_id])
        return found[0].price if found else None

    async def get_price_by_code(self, project_id: UUID, code: str) -> Price | None:
        entry = await get_entry(self._session, project_id, code)
        return None if entry is None else entry.price

    async def add_price(self, price: Price) -> None:
        entry = CatalogEntry(price, price.code, None, self._clock.now())
        if not await insert_price(self._session, entry):
            raise ValueError(f"price {price.code!r} already exists")

    async def add_version(self, price: Price, version: PriceVersion) -> None:
        await insert_version(self._session, price.project_id, version, self._clock.now())

    async def close_version(self, version: PriceVersion) -> None:
        statement = select(_version.c.project_id).where(_version.c.id == version.id)
        project_id = (await self._session.execute(statement)).scalar_one()
        await close_version(self._session, project_id, version)
