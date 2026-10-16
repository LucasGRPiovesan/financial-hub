"""Payments of a project: create (PIX, boleto, card), read, refund (ADR-0012, docs/DOMAIN.md s2).

``payments:write`` creates, ``payments:read`` reads, ``payments:refund`` refunds. Amounts are
integer minor units (never floats). Both POSTs require an ``Idempotency-Key``: a retried create
replays the first response instead of creating a second payment. A create whose provider outcome
is not known yet answers ``status: processing``; the resolver converges it.
"""

from datetime import date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, Field, StrictInt

from financial_hub.modules.audit.application import AuditContext, request_audit_context
from financial_hub.modules.identity.application.api import Permission
from financial_hub.modules.payments.application.registry import ProviderRegistry, build_registry
from financial_hub.modules.payments.application.service import (
    NewPayment,
    PaymentService,
    PaymentView,
    RefundView,
)
from financial_hub.platform.http.auth import ProjectScope, require
from financial_hub.platform.http.protection.idempotency import idempotent
from financial_hub.shared.pagination import Page, PageParams

router = APIRouter(prefix="/v1/projects/{project_id}/payments", tags=["payments"])


class RefundOut(BaseModel):
    id: str
    payment_id: str
    amount: int
    currency: str
    status: str
    origin: str
    reason: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, view: RefundView) -> RefundOut:
        return cls(
            id=view.id,
            payment_id=view.payment_id,
            amount=view.amount,
            currency=view.currency,
            status=view.status,
            origin=view.origin,
            reason=view.reason,
            created_at=view.created_at,
            updated_at=view.updated_at,
        )


class PaymentOut(BaseModel):
    id: str
    project_id: str
    customer_id: str
    amount: int = Field(description="Minor units (centavos for BRL).")
    currency: str
    method: str
    due_date: date
    external_ref: str | None
    description: str | None
    metadata: dict[str, Any]
    status: str = Field(description="Derived from the state fields (ADR-0012 precedence).")
    provider: str
    provider_account_id: str | None
    provider_payment_id: str | None
    capture_status: str
    settlement_status: str
    dispute_status: str
    refund_status: str
    captured_amount: int
    refunded_amount: int
    refundable_amount: int
    failure_code: str | None
    refunds: list[RefundOut]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, view: PaymentView) -> PaymentOut:
        return cls(
            id=view.id,
            project_id=view.project_id,
            customer_id=view.customer_id,
            amount=view.amount,
            currency=view.currency,
            method=view.method,
            due_date=view.due_date,
            external_ref=view.external_ref,
            description=view.description,
            metadata=view.metadata,
            status=view.status,
            provider=view.provider,
            provider_account_id=view.provider_account_id,
            provider_payment_id=view.provider_payment_id,
            capture_status=view.capture_status,
            settlement_status=view.settlement_status,
            dispute_status=view.dispute_status,
            refund_status=view.refund_status,
            captured_amount=view.captured_amount,
            refunded_amount=view.refunded_amount,
            refundable_amount=view.refundable_amount,
            failure_code=view.failure_code,
            refunds=[RefundOut.of(refund) for refund in view.refunds],
            created_at=view.created_at,
            updated_at=view.updated_at,
        )


class CustomerRef(BaseModel):
    id: str | None = Field(default=None, max_length=64, description="The `cus_` id.")
    external_ref: str | None = Field(default=None, max_length=128)


class CreatePaymentRequest(BaseModel):
    amount: StrictInt = Field(gt=0, description="Minor units; an integer, never a decimal.")
    currency: str = Field(default="BRL", min_length=3, max_length=3)
    method: str = Field(description="`pix`, `boleto` or `card`.")
    due_date: date
    customer: CustomerRef
    external_ref: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None, max_length=500)
    metadata: dict[str, Any] = Field(default_factory=dict[str, Any])
    provider_account_id: str | None = Field(
        default=None,
        max_length=64,
        description="Required only when the project has several active provider accounts.",
    )


class CreateRefundRequest(BaseModel):
    amount: StrictInt | None = Field(
        default=None, gt=0, description="Minor units; omit to refund everything refundable."
    )
    reason: str | None = Field(default=None, max_length=500)


def _registry(request: Request) -> ProviderRegistry:
    state = request.app.state
    registry: ProviderRegistry | None = getattr(state, "payment_providers", None)
    if registry is None:
        registry = build_registry(state.settings)
        state.payment_providers = registry
    return registry


def _service(request: Request, audit: AuditContext | None = None) -> PaymentService:
    state = request.app.state
    return PaymentService(
        state.session_factory, clock=state.clock, registry=_registry(request), audit=audit
    )


ReadPayments = Annotated[ProjectScope, require(Permission.PAYMENTS_READ)]
WritePayments = Annotated[ProjectScope, require(Permission.PAYMENTS_WRITE)]
RefundPayments = Annotated[ProjectScope, require(Permission.PAYMENTS_REFUND)]
AuditCtx = Annotated[AuditContext, Depends(request_audit_context)]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_payment(
    body: CreatePaymentRequest,
    scope: WritePayments,
    request: Request,
    audit: AuditCtx,
    _idempotency: Annotated[None, idempotent("required")],
) -> PaymentOut:
    """Create a payment and ask the provider for it (``processing`` until the outcome is known)."""
    view = await _service(request, audit).create(
        scope,
        NewPayment(
            amount=body.amount,
            currency=body.currency,
            method=body.method,
            due_date=body.due_date,
            customer_id=body.customer.id,
            customer_external_ref=body.customer.external_ref,
            external_ref=body.external_ref,
            description=body.description,
            metadata=body.metadata,
            provider_account_id=body.provider_account_id,
        ),
    )
    return PaymentOut.of(view)


@router.get("")
async def list_payments(
    scope: ReadPayments, params: Annotated[PageParams, Query()], request: Request
) -> Page[PaymentOut]:
    page = await _service(request).list_payments(scope.project_id, params)
    return Page[PaymentOut](
        data=[PaymentOut.of(view) for view in page.data],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


@router.get("/{payment_id}")
async def get_payment(payment_id: str, scope: ReadPayments, request: Request) -> PaymentOut:
    return PaymentOut.of(await _service(request).get(scope.project_id, payment_id))


@router.post("/{payment_id}/refunds", status_code=status.HTTP_201_CREATED)
async def create_refund(
    payment_id: str,
    body: CreateRefundRequest,
    scope: RefundPayments,
    request: Request,
    audit: AuditCtx,
    _idempotency: Annotated[None, idempotent("required")],
) -> RefundOut:
    """Refund all or part of a captured payment (at most what is still refundable)."""
    view = await _service(request, audit).refund(
        scope, payment_id, amount=body.amount, reason=body.reason
    )
    return RefundOut.of(view)
