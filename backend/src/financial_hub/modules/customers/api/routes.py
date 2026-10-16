"""Customers of a project: per-project identities of a universal customer.

``customers:manage`` (API-key scope ``customers:write``) writes, ``customers:read`` reads. Responses
carry the project's own identity and ``document_masked`` only: never the document, the universal
record or the person's other projects. Anonymization is operator-only.
"""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import BaseModel, Field

from financial_hub.modules.audit.application import AuditContext, request_audit_context
from financial_hub.modules.customers.application.service import CustomerService, CustomerView
from financial_hub.modules.identity.application.api import Permission
from financial_hub.platform.db import UnitOfWork
from financial_hub.platform.http.auth import ProjectScope, require
from financial_hub.platform.http.protection.idempotency import idempotent
from financial_hub.shared.pagination import Page, PageParams

router = APIRouter(prefix="/v1/projects/{project_id}/customers", tags=["customers"])


class CustomerOut(BaseModel):
    id: str
    project_id: str
    external_ref: str
    kind: str
    name: str
    email: str | None
    phone: str | None
    metadata: dict[str, Any]
    document_masked: str | None
    anonymized: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, view: CustomerView) -> CustomerOut:
        return cls(
            id=view.id,
            project_id=view.project_id,
            external_ref=view.external_ref,
            kind=view.kind,
            name=view.name,
            email=view.email,
            phone=view.phone,
            metadata=view.metadata,
            document_masked=view.document_masked,
            anonymized=view.anonymized,
            created_at=view.created_at,
            updated_at=view.updated_at,
        )


class UpsertCustomerRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    kind: str = Field(description="`individual` (CPF) or `company` (CNPJ).")
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=40)
    document: str | None = Field(
        default=None,
        max_length=32,
        description="CPF (11 digits) or CNPJ (14 digits), formatted or not. Never returned.",
    )
    metadata: dict[str, Any] = Field(default_factory=dict[str, Any])


def _uow(request: Request) -> UnitOfWork:
    return UnitOfWork(request.app.state.session_factory)


def _service(
    request: Request, uow: UnitOfWork, audit: AuditContext | None = None
) -> CustomerService:
    state = request.app.state
    return CustomerService(
        uow, clock=state.clock, keyring=state.keyring, pepper=state.api_key_pepper, audit=audit
    )


ReadCustomers = Annotated[ProjectScope, require(Permission.CUSTOMERS_READ)]
ManageCustomers = Annotated[ProjectScope, require(Permission.CUSTOMERS_MANAGE)]
AuditCtx = Annotated[AuditContext, Depends(request_audit_context)]


@router.put(
    "/{external_ref}",
    responses={status.HTTP_201_CREATED: {"model": CustomerOut, "description": "Created."}},
)
async def upsert_customer(
    external_ref: str,
    body: UpsertCustomerRequest,
    scope: ManageCustomers,
    request: Request,
    response: Response,
    audit: AuditCtx,
    _idempotency: Annotated[None, idempotent()],
) -> CustomerOut:
    """Create or update the project's customer identified by ``external_ref`` (200 on update)."""
    async with _uow(request) as uow:
        result = await _service(request, uow, audit).upsert(
            scope,
            external_ref,
            name=body.name,
            kind=body.kind,
            email=body.email,
            phone=body.phone,
            document=body.document,
            metadata=body.metadata,
        )
    if result.created:
        response.status_code = status.HTTP_201_CREATED
    return CustomerOut.of(result.customer)


class ListCustomersQuery(PageParams):
    external_ref: str | None = Field(default=None, max_length=128, description="Exact match.")
    email: str | None = Field(default=None, max_length=254, description="Exact match (any case).")


@router.get("")
async def list_customers(
    scope: ReadCustomers, params: Annotated[ListCustomersQuery, Query()], request: Request
) -> Page[CustomerOut]:
    async with _uow(request) as uow:
        page = await _service(request, uow).list_customers(
            scope.project_id, params, external_ref=params.external_ref, email=params.email
        )
    return Page[CustomerOut](
        data=[CustomerOut.of(view) for view in page.data],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


@router.get("/by-ref/{external_ref}")
async def get_customer_by_ref(
    external_ref: str, scope: ReadCustomers, request: Request
) -> CustomerOut:
    async with _uow(request) as uow:
        view = await _service(request, uow).get_by_ref(scope.project_id, external_ref)
    return CustomerOut.of(view)


@router.get("/{customer_id}")
async def get_customer(customer_id: str, scope: ReadCustomers, request: Request) -> CustomerOut:
    async with _uow(request) as uow:
        view = await _service(request, uow).get(scope.project_id, customer_id)
    return CustomerOut.of(view)


@router.post("/{customer_id}/anonymize")
async def anonymize_customer(
    customer_id: str, scope: ManageCustomers, request: Request, audit: AuditCtx
) -> CustomerOut:
    """Erase the customer's personal data (operators only; ledger entries are untouched)."""
    async with _uow(request) as uow:
        view = await _service(request, uow, audit).anonymize(scope, customer_id)
    return CustomerOut.of(view)
