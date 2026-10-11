"""Prices, immutable price versions with effective periods, and the per-project price book.

A ``Price`` is a named pricing definition (``code``) in one currency. Its ``PriceVersion`` values
carry the model and a half-open window ``[effective_from, effective_to)`` (``None`` = open-ended).
Windows of one price never overlap, so ``version_at`` is deterministic. Adding a version returns a
new ``Price``; existing versions are frozen values and never change. The single exception is
``close_open_version``, which sets the end of the open-ended version, and only in the future: an
instant that has already been rated against a version keeps resolving to it.
"""

import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Final, Self
from uuid import UUID

from financial_hub.modules.pricing.domain.model import PRICE_MODELS, PriceModel
from financial_hub.shared.errors import Conflict, FieldError, NotFound, ValidationFailed
from financial_hub.shared.money import Currency

CODE_PATTERN: Final = re.compile(r"[a-z0-9][a-z0-9_.-]{0,99}")


def _utc(value: object, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationFailed(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class PriceVersion:
    """Version ``version`` (1, 2, ...) of a price, effective on ``[from, to)``."""

    id: UUID
    price_id: UUID
    version: int
    model: PriceModel
    effective_from: datetime
    effective_to: datetime | None = None

    def __post_init__(self) -> None:
        if type(self.version) is not int:
            raise TypeError(f"version must be int, got {type(self.version).__name__}")
        if self.version < 1:
            raise ValidationFailed("version must be >= 1")
        if not isinstance(self.model, PRICE_MODELS):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError(f"model must be a price model, got {type(self.model).__name__}")
        object.__setattr__(self, "effective_from", _utc(self.effective_from, "effective_from"))
        if self.effective_to is not None:
            effective_to = _utc(self.effective_to, "effective_to")
            object.__setattr__(self, "effective_to", effective_to)
            if effective_to <= self.effective_from:
                raise ValidationFailed(
                    "effective_to must be after effective_from",
                    errors=[FieldError("/effective_to", "must be after effective_from")],
                )

    @property
    def currency(self) -> Currency:
        return self.model.currency

    def covers(self, instant: datetime) -> bool:
        at = _utc(instant, "instant")
        return self.effective_from <= at and (self.effective_to is None or at < self.effective_to)

    def overlaps(self, start: datetime, end: datetime | None) -> bool:
        """Whether ``[start, end)`` (``None`` = unbounded) intersects this version's window."""
        starts_before_end = end is None or self.effective_from < end
        ends_after_start = self.effective_to is None or start < self.effective_to
        return starts_before_end and ends_after_start


@dataclass(frozen=True, slots=True)
class Price:
    """A price ``code`` of a project with its versions, ordered by ``version``."""

    id: UUID
    project_id: UUID
    code: str
    currency: Currency
    versions: tuple[PriceVersion, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or CODE_PATTERN.fullmatch(self.code) is None:  # pyright: ignore[reportUnnecessaryIsInstance]
            raise ValidationFailed(
                f"price code must match {CODE_PATTERN.pattern}, got {self.code!r}",
                errors=[FieldError("/code", "invalid price code")],
            )
        versions = tuple(self.versions)
        object.__setattr__(self, "versions", versions)
        for index, version in enumerate(versions):
            if version.price_id != self.id:
                raise ValidationFailed(f"version {version.version} belongs to another price")
            if version.version != index + 1:
                raise ValidationFailed("versions must be numbered 1..n in order")
            if version.currency != self.currency:
                raise ValidationFailed(
                    f"version {version.version} is in {version.currency.code}, "
                    f"price is in {self.currency.code}"
                )
            for earlier in versions[:index]:
                if earlier.overlaps(version.effective_from, version.effective_to):
                    raise Conflict(f"version {version.version} overlaps version {earlier.version}")

    @property
    def latest(self) -> PriceVersion | None:
        return self.versions[-1] if self.versions else None

    def add_version(
        self,
        version_id: UUID,
        model: PriceModel,
        effective_from: datetime,
        effective_to: datetime | None = None,
    ) -> Self:
        """A new ``Price`` with version ``n + 1``; overlapping windows raise ``Conflict``."""
        version = PriceVersion(
            version_id, self.id, len(self.versions) + 1, model, effective_from, effective_to
        )
        return replace(self, versions=(*self.versions, version))

    def close_open_version(self, effective_to: datetime, *, now: datetime) -> Self:
        """End the open-ended version at ``effective_to``, which must not be in the past.

        Lets a new version follow an open-ended one without rewriting any instant that may
        already have been rated.
        """
        at = _utc(effective_to, "effective_to")
        if at < _utc(now, "now"):
            raise Conflict("an open-ended version can only be closed in the future")
        open_versions = [v for v in self.versions if v.effective_to is None]
        if not open_versions:
            raise Conflict("price has no open-ended version")
        closed = replace(open_versions[0], effective_to=at)
        return replace(
            self, versions=tuple(closed if v is open_versions[0] else v for v in self.versions)
        )

    def version_at(self, instant: datetime) -> PriceVersion:
        """The version effective at ``instant``; ``NotFound`` if none is."""
        for version in self.versions:
            if version.covers(instant):
                return version
        raise NotFound(f"price {self.code!r} has no version effective at {instant.isoformat()}")


@dataclass(frozen=True, slots=True)
class PriceBook:
    """All prices of one project, unique by ``code``."""

    project_id: UUID
    prices: tuple[Price, ...] = ()

    def __post_init__(self) -> None:
        prices = tuple(self.prices)
        object.__setattr__(self, "prices", prices)
        codes: set[str] = set()
        ids: set[UUID] = set()
        for price in prices:
            if price.project_id != self.project_id:
                raise ValidationFailed(f"price {price.code!r} belongs to another project")
            if price.code in codes or price.id in ids:
                raise Conflict(f"price {price.code!r} appears twice in the price book")
            codes.add(price.code)
            ids.add(price.id)

    def price(self, code: str) -> Price:
        for price in self.prices:
            if price.code == code:
                return price
        raise NotFound(f"no price {code!r} in this price book")

    def with_price(self, price: Price) -> Self:
        """A new book where ``price`` replaces the entry with the same id, or is added."""
        kept = tuple(p for p in self.prices if p.id != price.id)
        return replace(self, prices=(*kept, price))

    def resolve(self, code: str, instant: datetime) -> PriceVersion:
        return self.price(code).version_at(instant)
