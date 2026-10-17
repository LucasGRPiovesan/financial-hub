"""Price catalog: ``/v1/projects/{project_id}/prices`` (ADR-0007, docs/DOMAIN.md "Billing").

A price is addressed by its ``code``; its versions are immutable. The console manages prices
(``billing:manage``, operators only); reads need ``billing:read`` (operators or API keys).
"""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, Field

from financial_hub.modules.audit.application import AuditContext, request_audit_context
from financial_hub.modules.identity.application.api import Permission
from financial_hub.modules.pricing.application.api import (
    PriceCatalogService,
    PriceVersionView,
    PriceView,
)
from financial_hub.platform.db import UnitOfWork
from financial_hub.platform.http.auth import ProjectScope, require
from financial_hub.platform.http.problems import Problem
from financial_hub.shared.pagination import Page, PageParams

router = APIRouter(prefix="/v1/projects/{project_id}", tags=["pricing"])

ReadBilling = Annotated[ProjectScope, require(Permission.BILLING_READ)]
ManageBilling = Annotated[ProjectScope, require(Permission.BILLING_MANAGE)]
AuditCtx = Annotated[AuditContext, Depends(request_audit_context)]

MODEL_DESCRIPTION = (
    "The price model (`kind` + parameters). Quantities and unit prices are decimal strings in "
    'major units (`"0.0015"`); fixed amounts are integers in minor units (`4990`). Kinds: '
    "`per_unit`, `package`, `graduated`, `volume`, `percentage`, `flat_fee`, "
    "`base_fee_with_overage`, `minimum_commitment`."
)


class PriceVersionOut(BaseModel):
    version: int
    model: dict[str, Any]
    effective_from: datetime
    effective_to: datetime | None = Field(description="Exclusive end; null = open-ended.")

    @classmethod
    def of(cls, view: PriceVersionView) -> PriceVersionOut:
        return cls(
            version=view.version,
            model=view.model,
            effective_from=view.effective_from,
            effective_to=view.effective_to,
        )


class PriceOut(BaseModel):
    code: str
    name: str
    currency: str
    kind: str = Field(description="`fixed` (flat fee, percentage) or `usage` (rates a metric).")
    metric_code: str | None = Field(description="The billable metric a usage price rates.")
    created_at: datetime
    versions: list[PriceVersionOut]

    @classmethod
    def of(cls, view: PriceView) -> PriceOut:
        return cls(
            code=view.code,
            name=view.name,
            currency=view.currency,
            kind=view.kind,
            metric_code=view.metric_code,
            created_at=view.created_at,
            versions=[PriceVersionOut.of(version) for version in view.versions],
        )


class CreatePriceRequest(BaseModel):
    code: str = Field(max_length=100, description="Lowercase letters, digits, `_ . -`; unique.")
    name: str = Field(max_length=200)
    currency: str = Field(default="BRL", max_length=3)
    metric_code: str | None = Field(
        default=None, max_length=100, description="Required for usage models, absent otherwise."
    )
    model: dict[str, Any] = Field(description=MODEL_DESCRIPTION)
    effective_from: datetime | None = Field(
        default=None, description="Start of version 1 (with timezone). Defaults to now."
    )


class AddVersionRequest(BaseModel):
    model: dict[str, Any] = Field(description=MODEL_DESCRIPTION)
    effective_from: datetime = Field(
        description="Start of the new version, in the future; the current version ends there."
    )


def _service(
    request: Request, uow: UnitOfWork, audit: AuditContext | None = None
) -> PriceCatalogService:
    return PriceCatalogService(uow, clock=request.app.state.clock, audit=audit)


def _uow(request: Request) -> UnitOfWork:
    return UnitOfWork(request.app.state.session_factory)


@router.post(
    "/prices",
    status_code=status.HTTP_201_CREATED,
    responses={status.HTTP_409_CONFLICT: {"model": Problem, "description": "Code in use."}},
)
async def create_price(
    body: CreatePriceRequest, scope: ManageBilling, request: Request, audit: AuditCtx
) -> PriceOut:
    """Create a price with its first version."""
    async with _uow(request) as uow:
        view = await _service(request, uow, audit).create_price(
            scope,
            code=body.code,
            name=body.name,
            currency=body.currency,
            model=body.model,
            metric_code=body.metric_code,
            effective_from=body.effective_from,
        )
    return PriceOut.of(view)


@router.get("/prices")
async def list_prices(
    scope: ReadBilling, params: Annotated[PageParams, Query()], request: Request
) -> Page[PriceOut]:
    async with _uow(request) as uow:
        page = await _service(request, uow).list_prices(scope.project_id, params)
    return Page[PriceOut](
        data=[PriceOut.of(view) for view in page.data],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


@router.get("/prices/{code}")
async def get_price(code: str, scope: ReadBilling, request: Request) -> PriceOut:
    async with _uow(request) as uow:
        view = await _service(request, uow).get_price(scope.project_id, code)
    return PriceOut.of(view)


@router.post(
    "/prices/{code}/versions",
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_409_CONFLICT: {"model": Problem, "description": "A later version exists."}
    },
)
async def add_price_version(
    code: str, body: AddVersionRequest, scope: ManageBilling, request: Request, audit: AuditCtx
) -> PriceOut:
    """Add a version from a future date. History is never rewritten."""
    async with _uow(request) as uow:
        view = await _service(request, uow, audit).add_version(
            scope, code, model=body.model, effective_from=body.effective_from
        )
    return PriceOut.of(view)
