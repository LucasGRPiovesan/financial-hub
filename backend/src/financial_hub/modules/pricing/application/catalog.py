"""The price catalog: per-project prices and their immutable versions (ADR-0007, W5.3).

Rules:

- A price is addressed by its ``code`` and never changes; its versions are append-only.
- A **usage** price (per-unit, package, graduated, volume, base fee with overage, or a minimum
  commitment over one of those) pins the billable ``metric_code`` it rates; a **fixed** price
  (flat fee, percentage) has none. Later versions keep the price's kind.
- The first version starts at ``effective_from`` (default: now). A new version must start in the
  future: the open-ended version is closed at that instant and the new one starts there, so the
  windows stay contiguous and nothing already rated changes meaning. Writers of one price are
  serialized by locking its row; the database also refuses overlapping windows.
- Changes are operator-only (``billing:manage``); reads need ``billing:read`` (keys included).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from financial_hub.modules.audit.application import (
    Actor,
    AuditContext,
    AuditRecorder,
    AuditTarget,
)
from financial_hub.modules.pricing.domain import (
    FlatFee,
    MinimumCommitment,
    Percentage,
    Price,
    PriceModel,
    PriceVersion,
)
from financial_hub.modules.pricing.infrastructure import repository
from financial_hub.modules.pricing.infrastructure.codec import model_from_json, model_to_json
from financial_hub.modules.pricing.infrastructure.repository import CatalogEntry
from financial_hub.platform.db import UnitOfWork
from financial_hub.platform.http.auth import ProjectScope
from financial_hub.shared.clock import Clock
from financial_hub.shared.errors import Conflict, FieldError, Forbidden, NotFound, ValidationFailed
from financial_hub.shared.ids import new_uuid
from financial_hub.shared.money import Currency
from financial_hub.shared.pagination import Cursor, Page, PageParams, build_page

API_KEY_KIND: Final = "api_key"
NAME_MAX_LENGTH: Final = 200
_METRIC_FK: Final = "fk_prices_project_id_metric_code_billable_metrics"


class PriceCodeTaken(Conflict):
    """The project already has a price with this code (409)."""

    slug = "price-code-taken"
    title = "Price code already in use"


@dataclass(frozen=True, slots=True)
class PriceVersionView:
    id: UUID
    version: int
    model: dict[str, Any]
    effective_from: datetime
    effective_to: datetime | None


@dataclass(frozen=True, slots=True)
class PriceView:
    id: UUID
    code: str
    name: str
    currency: str
    kind: str
    metric_code: str | None
    created_at: datetime
    versions: tuple[PriceVersionView, ...]


def requires_metric(model: PriceModel) -> bool:
    """Whether ``model`` rates a metered quantity (and so needs a ``metric_code``)."""
    inner = model.inner if isinstance(model, MinimumCommitment) else model
    return not isinstance(inner, (FlatFee, Percentage))


def _invalid(pointer: str, detail: str) -> ValidationFailed:
    return ValidationFailed(
        "The request contains invalid fields.", errors=[FieldError(pointer=pointer, detail=detail)]
    )


def operator_only(scope: ProjectScope) -> None:
    """Catalog and invoice changes come from the console, never from an API key."""
    if scope.principal_kind == API_KEY_KIND:
        raise Forbidden("Only an operator can change the price catalog.")


def price_view(entry: CatalogEntry) -> PriceView:
    price = entry.price
    return PriceView(
        id=price.id,
        code=price.code,
        name=entry.name,
        currency=price.currency.code,
        kind="usage" if entry.metric_code is not None else "fixed",
        metric_code=entry.metric_code,
        created_at=entry.created_at,
        versions=tuple(
            PriceVersionView(
                id=version.id,
                version=version.version,
                model=model_to_json(version.model),
                effective_from=version.effective_from,
                effective_to=version.effective_to,
            )
            for version in price.versions
        ),
    )


def _aware(value: datetime, pointer: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise _invalid(pointer, "must include a timezone offset")
    return value


class PriceCatalogService:
    """Every method runs inside the caller's active :class:`UnitOfWork`."""

    def __init__(self, uow: UnitOfWork, *, clock: Clock, audit: AuditContext | None = None) -> None:
        self._uow = uow
        self._clock = clock
        self._audit = audit or AuditContext.system()

    async def _record(
        self, scope: ProjectScope, action: str, code: str, diff: dict[str, Any]
    ) -> None:
        await AuditRecorder.record(
            self._uow.session,
            actor=self._audit.acting(Actor.of_principal(scope.principal_kind, scope.principal_id)),
            action=action,
            target=AuditTarget("price", code),
            diff=diff,
            project_id=scope.project_id,
        )

    async def create_price(
        self,
        scope: ProjectScope,
        *,
        code: str,
        name: str,
        currency: str,
        model: object,
        metric_code: str | None = None,
        effective_from: datetime | None = None,
    ) -> PriceView:
        operator_only(scope)
        clean_name = name.strip()
        if not 1 <= len(clean_name) <= NAME_MAX_LENGTH:
            raise _invalid("/name", f"must be 1-{NAME_MAX_LENGTH} characters")
        try:
            money_currency = Currency.of(currency)
        except ValueError:
            raise _invalid("/currency", "unknown ISO 4217 currency") from None
        parsed = model_from_json(model, money_currency)
        if requires_metric(parsed) and metric_code is None:
            raise _invalid("/metric_code", f"a {parsed.kind} price needs the metric it rates")
        if not requires_metric(parsed) and metric_code is not None:
            raise _invalid("/metric_code", f"a {parsed.kind} price rates no metric")
        now = self._clock.now()
        start = now if effective_from is None else _aware(effective_from, "/effective_from")
        price_id = new_uuid()
        price = Price(price_id, scope.project_id, code, money_currency).add_version(
            new_uuid(), parsed, start
        )
        entry = CatalogEntry(price, clean_name, metric_code, now)
        try:
            inserted = await repository.insert_price(self._uow.session, entry)
        except IntegrityError as error:
            if _METRIC_FK in str(error.orig):
                raise _invalid("/metric_code", "no billable metric with this code") from None
            raise
        if not inserted:
            raise PriceCodeTaken(f"The project already has a price with code {code}.")
        await self._record(
            scope,
            "price.created",
            code,
            {
                "currency": money_currency.code,
                "metric_code": metric_code,
                "model": model_to_json(parsed),
                "effective_from": start.isoformat(),
            },
        )
        return price_view(entry)

    async def _require(self, project_id: UUID, code: str, *, lock: bool = False) -> CatalogEntry:
        entry = await repository.get_entry(self._uow.session, project_id, code, lock=lock)
        if entry is None:
            raise NotFound("Price not found.")
        return entry

    async def get_price(self, project_id: UUID, code: str) -> PriceView:
        return price_view(await self._require(project_id, code))

    async def list_prices(self, project_id: UUID, params: PageParams) -> Page[PriceView]:
        rows = await repository.list_entries(
            self._uow.session, project_id, cursor=params.decoded_cursor(), limit=params.limit + 1
        )
        return build_page(
            rows,
            params.limit,
            price_view,
            lambda entry: Cursor(created_at=entry.created_at, id=entry.price.id),
        )

    async def add_version(
        self, scope: ProjectScope, code: str, *, model: object, effective_from: datetime
    ) -> PriceView:
        """Version ``n + 1`` from ``effective_from`` (future); the open version ends there."""
        operator_only(scope)
        start = _aware(effective_from, "/effective_from")
        now = self._clock.now()
        if start <= now:
            raise _invalid("/effective_from", "a new version must start in the future")
        entry = await self._require(scope.project_id, code, lock=True)
        price = entry.price
        parsed = model_from_json(model, price.currency)
        if requires_metric(parsed) != (entry.metric_code is not None):
            kind = "usage" if entry.metric_code is not None else "fixed"
            raise _invalid("/model/kind", f"this is a {kind} price; a {parsed.kind} model is not")
        latest = price.latest
        if latest is None:  # a price is created with its first version
            raise NotFound("Price has no version.")
        if latest.effective_to is not None or latest.effective_from >= start:
            raise Conflict("A later version is already scheduled for this price.")
        closed = price.close_open_version(start, now=now)
        closing = closed.versions[-1]
        updated = closed.add_version(new_uuid(), parsed, start)
        if not await repository.close_version(self._uow.session, scope.project_id, closing):
            raise Conflict("The price changed concurrently; retry.")
        added: PriceVersion = updated.versions[-1]
        await repository.insert_version(self._uow.session, scope.project_id, added, now)
        await self._record(
            scope,
            "price.version_added",
            code,
            {
                "version": added.version,
                "model": model_to_json(parsed),
                "effective_from": start.isoformat(),
            },
        )
        return price_view(CatalogEntry(updated, entry.name, entry.metric_code, entry.created_at))
