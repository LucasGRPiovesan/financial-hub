"""Plans, subscriptions and invoices: ``/v1/projects/{project_id}/...`` (docs/DOMAIN.md "Billing").

Separation of duties (docs/SECURITY.md): ``billing:manage`` (owner, finance) changes plans,
subscriptions and invoices; ``billing:read`` reads them. Plan and invoice changes are made by an
operator in the console, never by an API key; a key holding ``billing:manage`` may subscribe and
cancel customers. Amounts are integers in minor units.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, Field

from financial_hub.modules.audit.application import AuditContext, request_audit_context
from financial_hub.modules.billing.application.api import (
    BillingService,
    Invoice,
    InvoiceLine,
    PlanView,
    SubscriptionView,
)
from financial_hub.modules.identity.application.api import Permission
from financial_hub.modules.payments.application.api import PaymentService, ProviderRegistry
from financial_hub.modules.payments.application.api import build_registry as build_payments
from financial_hub.platform.http.auth import ProjectScope, require
from financial_hub.platform.http.problems import Problem
from financial_hub.shared.errors import NotFound
from financial_hub.shared.ids import IdPrefix, InvalidIdError, format_id, parse_id
from financial_hub.shared.pagination import Page, PageParams

router = APIRouter(prefix="/v1/projects/{project_id}", tags=["billing"])

ReadBilling = Annotated[ProjectScope, require(Permission.BILLING_READ)]
ManageBilling = Annotated[ProjectScope, require(Permission.BILLING_MANAGE)]
AuditCtx = Annotated[AuditContext, Depends(request_audit_context)]

CONFLICT: dict[int | str, dict[str, Any]] = {
    status.HTTP_409_CONFLICT: {"model": Problem, "description": "State conflict."}
}


def _registry(request: Request) -> ProviderRegistry:
    state = request.app.state
    registry: ProviderRegistry | None = getattr(state, "payment_providers", None)
    if registry is None:
        registry = build_payments(state.settings)
        state.payment_providers = registry
    return registry


def _service(request: Request, audit: AuditContext | None = None) -> BillingService:
    state = request.app.state
    payments = PaymentService(
        state.session_factory,
        clock=state.clock,
        registry=_registry(request),
        # set by the composition root (main.py); absent only in tests that use the fake provider
        credentials=getattr(state, "payment_credentials", None),
        contacts=getattr(state, "payment_contacts", None),
        audit=audit,
    )
    return BillingService(state.session_factory, clock=state.clock, payments=payments, audit=audit)


def _parse(value: str, prefix: IdPrefix, what: str) -> UUID:
    try:
        return parse_id(value, prefix)
    except InvalidIdError:
        raise NotFound(f"{what} not found.") from None


# --- plans --------------------------------------------------------------------------------------


class PlanOut(BaseModel):
    code: str
    name: str
    currency: str
    billing_interval: str = Field(description="Always `month`.")
    prices: list[str] = Field(description="The price codes billed every period, in order.")
    created_at: datetime

    @classmethod
    def of(cls, view: PlanView) -> PlanOut:
        plan = view.plan
        return cls(
            code=plan.code,
            name=plan.name,
            currency=plan.currency,
            billing_interval=plan.billing_interval,
            prices=list(view.price_codes),
            created_at=plan.created_at,
        )


class CreatePlanRequest(BaseModel):
    code: str = Field(max_length=100, description="Lowercase letters, digits, `_ . -`; unique.")
    name: str = Field(max_length=200)
    currency: str = Field(default="BRL", max_length=3)
    prices: list[str] = Field(
        min_length=1, max_length=20, description="Price codes in the plan's currency."
    )


@router.post("/plans", status_code=status.HTTP_201_CREATED, responses=CONFLICT)
async def create_plan(
    body: CreatePlanRequest, scope: ManageBilling, request: Request, audit: AuditCtx
) -> PlanOut:
    """Create a monthly plan from catalog prices (operators only)."""
    view = await _service(request, audit).create_plan(
        scope, code=body.code, name=body.name, price_codes=body.prices, currency=body.currency
    )
    return PlanOut.of(view)


@router.get("/plans")
async def list_plans(
    scope: ReadBilling, params: Annotated[PageParams, Query()], request: Request
) -> Page[PlanOut]:
    page = await _service(request).list_plans(scope.project_id, params)
    return Page[PlanOut](
        data=[PlanOut.of(view) for view in page.data],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


@router.get("/plans/{code}")
async def get_plan(code: str, scope: ReadBilling, request: Request) -> PlanOut:
    return PlanOut.of(await _service(request).get_plan(scope.project_id, code))


# --- subscriptions ------------------------------------------------------------------------------


class SubscriptionOut(BaseModel):
    id: str
    customer_id: str
    plan: str
    status: str = Field(description="`trialing`, `active` or `canceled`.")
    anchor_day: int
    started_at: datetime
    trial_end: datetime | None
    current_period_start: datetime
    current_period_end: datetime = Field(description="Exclusive end of the current period.")
    cancel_at_period_end: bool
    canceled_at: datetime | None
    ended_at: datetime | None
    payment_method: str
    days_until_due: int
    created_at: datetime

    @classmethod
    def of(cls, view: SubscriptionView) -> SubscriptionOut:
        subscription = view.subscription
        return cls(
            id=format_id(IdPrefix.SUBSCRIPTION, subscription.id),
            customer_id=format_id(IdPrefix.CUSTOMER, subscription.customer_id),
            plan=view.plan_code,
            status=subscription.status.value,
            anchor_day=subscription.anchor_day,
            started_at=subscription.started_at,
            trial_end=subscription.trial_end,
            current_period_start=subscription.current_period_start,
            current_period_end=subscription.current_period_end,
            cancel_at_period_end=subscription.cancel_at_period_end,
            canceled_at=subscription.canceled_at,
            ended_at=subscription.ended_at,
            payment_method=subscription.payment_method,
            days_until_due=subscription.days_until_due,
            created_at=subscription.created_at,
        )


class CreateSubscriptionRequest(BaseModel):
    customer_id: str = Field(max_length=64, description="The `cus_` id of the customer.")
    plan: str = Field(max_length=100, description="The plan code.")
    trial_days: int = Field(default=0, ge=0, le=365)
    anchor_day: int | None = Field(
        default=None, ge=1, le=31, description="Day of the month periods start (UTC)."
    )
    payment_method: str = Field(default="pix", description="`pix`, `boleto` or `card`.")
    days_until_due: int = Field(default=7, ge=0, le=60)


class CancelSubscriptionRequest(BaseModel):
    at_period_end: bool = Field(
        default=True,
        description="End at the current period's end (default) or now (final prorated invoice).",
    )


@router.post("/subscriptions", status_code=status.HTTP_201_CREATED, responses=CONFLICT)
async def create_subscription(
    body: CreateSubscriptionRequest, scope: ManageBilling, request: Request, audit: AuditCtx
) -> SubscriptionOut:
    """Subscribe a customer to a plan from now."""
    view = await _service(request, audit).create_subscription(
        scope,
        customer_id=body.customer_id,
        plan_code=body.plan,
        trial_days=body.trial_days,
        anchor_day=body.anchor_day,
        payment_method=body.payment_method,
        days_until_due=body.days_until_due,
    )
    return SubscriptionOut.of(view)


class ListSubscriptionsQuery(PageParams):
    customer_id: str | None = Field(default=None, max_length=64, description="Exact match.")


@router.get("/subscriptions")
async def list_subscriptions(
    scope: ReadBilling, params: Annotated[ListSubscriptionsQuery, Query()], request: Request
) -> Page[SubscriptionOut]:
    page = await _service(request).list_subscriptions(
        scope.project_id, params, customer_id=params.customer_id
    )
    return Page[SubscriptionOut](
        data=[SubscriptionOut.of(view) for view in page.data],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


@router.get("/subscriptions/{subscription_id}")
async def get_subscription(
    subscription_id: str, scope: ReadBilling, request: Request
) -> SubscriptionOut:
    view = await _service(request).get_subscription(
        scope.project_id, _parse(subscription_id, IdPrefix.SUBSCRIPTION, "Subscription")
    )
    return SubscriptionOut.of(view)


@router.post("/subscriptions/{subscription_id}/cancel", responses=CONFLICT)
async def cancel_subscription(
    subscription_id: str,
    body: CancelSubscriptionRequest,
    scope: ManageBilling,
    request: Request,
    audit: AuditCtx,
) -> SubscriptionOut:
    view = await _service(request, audit).cancel_subscription(
        scope,
        _parse(subscription_id, IdPrefix.SUBSCRIPTION, "Subscription"),
        at_period_end=body.at_period_end,
    )
    return SubscriptionOut.of(view)


# --- invoices -----------------------------------------------------------------------------------


class InvoiceLineOut(BaseModel):
    kind: str = Field(description="`fixed` or `usage`.")
    price: str = Field(description="The price code.")
    price_version: int
    model: str
    metric_code: str | None
    quantity: Decimal | None = Field(description="Billed usage (usage lines).")
    amount: int = Field(description="Minor units.")
    rounding: str
    details: dict[str, Any]

    @classmethod
    def of(cls, line: InvoiceLine) -> InvoiceLineOut:
        return cls(
            kind=line.kind.value,
            price=line.price_code,
            price_version=line.price_version,
            model=line.model,
            metric_code=line.metric_code,
            quantity=line.quantity,
            amount=line.amount,
            rounding=line.rounding,
            details=dict(line.details),
        )


class InvoiceOut(BaseModel):
    id: str
    subscription_id: str
    customer_id: str
    currency: str
    status: str = Field(description="`draft`, `finalized`, `paid`, `void` or `uncollectible`.")
    period_start: datetime
    period_end: datetime = Field(description="Exclusive end of the billed period.")
    total: int = Field(description="Minor units: the sum of the lines.")
    credits_applied: int
    amount_due: int = Field(description="`total - credits_applied`, collected by the payment.")
    payment_id: str | None = Field(description="The `pi_` payment collecting the amount due.")
    due_date: date | None
    finalized_at: datetime | None
    paid_at: datetime | None
    voided_at: datetime | None
    created_at: datetime
    lines: list[InvoiceLineOut]

    @classmethod
    def of(cls, invoice: Invoice) -> InvoiceOut:
        return cls(
            id=format_id(IdPrefix.INVOICE, invoice.id),
            subscription_id=format_id(IdPrefix.SUBSCRIPTION, invoice.subscription_id),
            customer_id=format_id(IdPrefix.CUSTOMER, invoice.customer_id),
            currency=invoice.currency,
            status=invoice.status.value,
            period_start=invoice.period_start,
            period_end=invoice.period_end,
            total=invoice.total,
            credits_applied=invoice.credits_applied,
            amount_due=invoice.amount_due,
            payment_id=(
                None
                if invoice.payment_intent_id is None
                else format_id(IdPrefix.PAYMENT_INTENT, invoice.payment_intent_id)
            ),
            due_date=invoice.due_date,
            finalized_at=invoice.finalized_at,
            paid_at=invoice.paid_at,
            voided_at=invoice.voided_at,
            created_at=invoice.created_at,
            lines=[InvoiceLineOut.of(line) for line in invoice.lines],
        )


class ListInvoicesQuery(PageParams):
    subscription_id: str | None = Field(default=None, max_length=64, description="Exact match.")


@router.get("/invoices")
async def list_invoices(
    scope: ReadBilling, params: Annotated[ListInvoicesQuery, Query()], request: Request
) -> Page[InvoiceOut]:
    subscription = (
        None
        if params.subscription_id is None
        else _parse(params.subscription_id, IdPrefix.SUBSCRIPTION, "Subscription")
    )
    page = await _service(request).list_invoices(
        scope.project_id, params, subscription_id=subscription
    )
    return Page[InvoiceOut](
        data=[InvoiceOut.of(invoice) for invoice in page.data],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


@router.get("/invoices/{invoice_id}")
async def get_invoice(invoice_id: str, scope: ReadBilling, request: Request) -> InvoiceOut:
    invoice = await _service(request).get_invoice(
        scope.project_id, _parse(invoice_id, IdPrefix.INVOICE, "Invoice")
    )
    return InvoiceOut.of(invoice)


@router.post("/invoices/{invoice_id}/finalize", responses=CONFLICT)
async def finalize_invoice(
    invoice_id: str, scope: ManageBilling, request: Request, audit: AuditCtx
) -> InvoiceOut:
    """Finalize a draft: books the receivable and requests its payment (operators only)."""
    invoice = await _service(request, audit).finalize_invoice(
        scope, _parse(invoice_id, IdPrefix.INVOICE, "Invoice")
    )
    return InvoiceOut.of(invoice)


@router.post("/invoices/{invoice_id}/void", responses=CONFLICT)
async def void_invoice(
    invoice_id: str, scope: ManageBilling, request: Request, audit: AuditCtx
) -> InvoiceOut:
    """Void an invoice nothing was captured for: cancels its payment (operators only)."""
    invoice = await _service(request, audit).void_invoice(
        scope, _parse(invoice_id, IdPrefix.INVOICE, "Invoice")
    )
    return InvoiceOut.of(invoice)
