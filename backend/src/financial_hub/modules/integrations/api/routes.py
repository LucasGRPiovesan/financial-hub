"""Integration manifests (docs/INTEGRATIONS.md §1).

- ``GET /v1/manifest-schema/v1``: the ``manifest.v1`` JSON Schema (draft 2020-12), any session or
  key.
- ``/v1/projects/{project_id}/manifests``: drafting, editing and validating versions is
  ``integrations:manage``; approving, rejecting and activating is ``integrations:approve`` (owner,
  finance) and needs an operator session; a version that changes prices, splits or the wallet
  needs a second operator (four-eyes). Reading and the dry run (``simulate``, no writes) is
  ``integrations:read``.
- ``POST /v1/business-events``: a project **API key** with the ``events:write`` scope sends a
  business event, mapped to usage by the active manifest. Idempotent by ``(name,
  idempotency_key)``: the same payload answers 200 with the original, a different one 409.
"""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import BaseModel, Field

from financial_hub.modules.audit.application import AuditContext, request_audit_context
from financial_hub.modules.identity.application.api import Permission
from financial_hub.modules.integrations.application.api import (
    Activation,
    ActivationResult,
    BusinessEventRecord,
    BusinessEventService,
    ManifestService,
    ManifestVersion,
    Simulation,
    manifest_json_schema,
    manifest_public_id,
)
from financial_hub.platform.db import UnitOfWork
from financial_hub.platform.http.auth import Principal, ProjectScope, authenticated, require
from financial_hub.platform.http.problems import Problem
from financial_hub.shared.errors import Forbidden, NotFound
from financial_hub.shared.money import Money
from financial_hub.shared.pagination import Page, PageParams

# The scheme name of project API keys (``projects.application.api_keys.API_KEY_PRINCIPAL``).
API_KEY_KIND = "api_key"

schema_router = APIRouter(prefix="/v1/manifest-schema", tags=["integrations"])
manifests_router = APIRouter(prefix="/v1/projects/{project_id}", tags=["integrations"])
business_events_router = APIRouter(prefix="/v1/business-events", tags=["integrations"])


class ManifestOut(BaseModel):
    id: str
    version: int
    status: str = Field(
        description="`draft`, `validated`, `approved`, `active`, `superseded` or `rejected`."
    )
    content: dict[str, Any]
    content_hash: str = Field(description="SHA-256 of the canonical JSON; approve it verbatim.")
    created_by: str
    created_at: datetime
    updated_at: datetime
    validated_at: datetime | None
    approved_by: str | None
    approved_at: datetime | None
    four_eyes: bool | None = Field(description="Approved by an operator other than the author.")
    approval_base_version: int | None
    approval_diff: dict[str, Any] | None
    rejected_by: str | None
    rejected_at: datetime | None
    rejection_reason: str | None
    activated_at: datetime | None
    superseded_at: datetime | None

    @classmethod
    def of(cls, manifest: ManifestVersion) -> ManifestOut:
        return cls(
            id=manifest_public_id(manifest.id),
            version=manifest.version,
            status=manifest.status.value,
            content=manifest.content,
            content_hash=manifest.content_hash,
            created_by=manifest.created_by,
            created_at=manifest.created_at,
            updated_at=manifest.updated_at,
            validated_at=manifest.validated_at,
            approved_by=manifest.approved_by,
            approved_at=manifest.approved_at,
            four_eyes=manifest.four_eyes,
            approval_base_version=manifest.approval_base_version,
            approval_diff=manifest.approval_diff,
            rejected_by=manifest.rejected_by,
            rejected_at=manifest.rejected_at,
            rejection_reason=manifest.rejection_reason,
            activated_at=manifest.activated_at,
            superseded_at=manifest.superseded_at,
        )


class ActivationOut(BaseModel):
    version: int
    previous_version: int | None
    activated_by: str
    activated_at: datetime
    materialized: dict[str, Any] = Field(
        description="Metrics (`existing`, `reused`, `created`) and webhook endpoint ids."
    )

    @classmethod
    def of(cls, activation: Activation) -> ActivationOut:
        return cls(
            version=activation.version,
            previous_version=activation.previous_version,
            activated_by=activation.activated_by,
            activated_at=activation.activated_at,
            materialized=activation.materialized,
        )


class EndpointSecretOut(BaseModel):
    endpoint_id: str
    url: str
    secret: str = Field(description="Signing secret; shown once, never stored in the manifest.")


class ActivateOut(BaseModel):
    manifest: ManifestOut
    activation: ActivationOut
    webhook_secrets: list[EndpointSecretOut] = Field(
        description="Secrets of the endpoints this activation created (only in this response)."
    )

    @classmethod
    def of(cls, result: ActivationResult) -> ActivateOut:
        return cls(
            manifest=ManifestOut.of(result.manifest),
            activation=ActivationOut.of(result.activation),
            webhook_secrets=[
                EndpointSecretOut(endpoint_id=item.endpoint_id, url=item.url, secret=item.secret)
                for item in result.secrets
            ],
        )


class ContentRequest(BaseModel):
    content: dict[str, Any] = Field(description="A `manifest.v1` document.")


class ApproveRequest(BaseModel):
    content_hash: str = Field(
        min_length=64, max_length=64, description="The `content_hash` you reviewed."
    )


class RejectRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


class SimulateRequest(BaseModel):
    name: str = Field(max_length=100, description="A business event the version declares.")
    payload: dict[str, Any]
    idempotency_key: str = Field(default="simulation", max_length=64)


class MoneyOut(BaseModel):
    amount_minor: int = Field(description="Integer minor units of the currency (no float).")
    currency: str = Field(description="ISO 4217 code, e.g. BRL.")

    @classmethod
    def of(cls, money: Money) -> MoneyOut:
        return cls(amount_minor=money.minor_units, currency=money.currency.code)


class UsageDraftOut(BaseModel):
    transaction_id: str
    metric: str
    customer_external_ref: str
    properties: dict[str, Any]
    timestamp: datetime | None


class ChargeLineOut(BaseModel):
    key: str
    quantity: str
    unit_amount: str
    amount: MoneyOut


class ChargeOut(BaseModel):
    transaction_id: str
    metric: str
    price: str
    price_version: int
    lines: list[ChargeLineOut]
    amount: MoneyOut


class AllocationOut(BaseModel):
    payee: str = Field(description="A split payee, or `platform` (always last: the remainder).")
    basis_points: int
    amount: MoneyOut


class SimulationOut(BaseModel):
    version: int
    event: str
    usage: list[UsageDraftOut]
    charges: list[ChargeOut]
    allocations: dict[str, list[AllocationOut]] = Field(
        description="Per currency: largest-remainder split of the rated total, platform last."
    )
    notes: list[str]

    @classmethod
    def of(cls, result: Simulation) -> SimulationOut:
        return cls(
            version=result.version,
            event=result.event,
            usage=[
                UsageDraftOut(
                    transaction_id=draft.transaction_id,
                    metric=draft.metric,
                    customer_external_ref=draft.customer_external_ref,
                    properties=draft.properties,
                    timestamp=draft.timestamp,
                )
                for draft in result.usage
            ],
            charges=[
                ChargeOut(
                    transaction_id=charge.transaction_id,
                    metric=charge.metric,
                    price=charge.price,
                    price_version=charge.price_version,
                    lines=[
                        ChargeLineOut(
                            key=line.key,
                            quantity=str(line.quantity),
                            unit_amount=str(line.unit_amount),
                            amount=MoneyOut.of(line.amount),
                        )
                        for line in charge.lines
                    ],
                    amount=MoneyOut.of(charge.amount),
                )
                for charge in result.charges
            ],
            allocations={
                currency: [
                    AllocationOut(
                        payee=part.payee,
                        basis_points=part.basis_points,
                        amount=MoneyOut.of(part.amount),
                    )
                    for part in parts
                ]
                for currency, parts in result.allocations.items()
            },
            notes=result.notes,
        )


class BusinessEventRequest(BaseModel):
    name: str = Field(max_length=100, description="A business event of the active manifest.")
    payload: dict[str, Any]
    idempotency_key: str = Field(
        max_length=64, description="Your unique id of this event, per project and name."
    )


class BusinessEventOut(BaseModel):
    name: str
    idempotency_key: str
    manifest_version: int
    payload: dict[str, Any] = Field(description="As stored: personal data redacted.")
    outcome: dict[str, Any] = Field(description="The usage events it recorded.")
    received_at: datetime

    @classmethod
    def of(cls, record: BusinessEventRecord) -> BusinessEventOut:
        return cls(
            name=record.name,
            idempotency_key=record.idempotency_key,
            manifest_version=record.manifest_version,
            payload=record.payload,
            outcome=record.outcome,
            received_at=record.received_at,
        )


def _uow(request: Request) -> UnitOfWork:
    return UnitOfWork(request.app.state.session_factory)


def _service(
    request: Request, uow: UnitOfWork, audit: AuditContext | None = None
) -> ManifestService:
    state = request.app.state
    return ManifestService(
        uow,
        clock=state.clock,
        audit=audit,
        keyring=state.keyring,
        webhook_policy=state.webhook_policy,
    )


ReadIntegrations = Annotated[ProjectScope, require(Permission.INTEGRATIONS_READ)]
ManageIntegrations = Annotated[ProjectScope, require(Permission.INTEGRATIONS_MANAGE)]
ApproveIntegrations = Annotated[ProjectScope, require(Permission.INTEGRATIONS_APPROVE)]
AuditCtx = Annotated[AuditContext, Depends(request_audit_context)]
Caller = Annotated[Principal, authenticated()]


# -- schema ------------------------------------------------------------------------------------


@schema_router.get("/v1")
async def manifest_schema(principal: Caller) -> dict[str, Any]:
    """The ``manifest.v1`` JSON Schema (draft 2020-12). Validation adds cross-field rules."""
    del principal
    return manifest_json_schema()


# -- versions ----------------------------------------------------------------------------------


@manifests_router.post(
    "/manifests",
    status_code=status.HTTP_201_CREATED,
    responses={422: {"model": Problem}},
)
async def create_manifest(
    body: ContentRequest, scope: ManageIntegrations, request: Request, audit: AuditCtx
) -> ManifestOut:
    """Create a draft with the next version number. Validation is a separate step."""
    async with _uow(request) as uow:
        manifest = await _service(request, uow, audit).create(scope, body.content)
    return ManifestOut.of(manifest)


@manifests_router.get("/manifests")
async def list_manifests(
    scope: ReadIntegrations, params: Annotated[PageParams, Query()], request: Request
) -> Page[ManifestOut]:
    async with _uow(request) as uow:
        page = await _service(request, uow).list(scope.project_id, params)
    return Page[ManifestOut](
        data=[ManifestOut.of(item) for item in page.data],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


@manifests_router.get("/manifests/active")
async def active_manifest(scope: ReadIntegrations, request: Request) -> ManifestOut:
    async with _uow(request) as uow:
        manifest = await _service(request, uow).active(scope.project_id)
    if manifest is None:
        raise NotFound("The project has no active manifest.")
    return ManifestOut.of(manifest)


@manifests_router.get("/manifests/activations")
async def list_activations(scope: ReadIntegrations, request: Request) -> list[ActivationOut]:
    """The latest activations, newest first (rollbacks included)."""
    async with _uow(request) as uow:
        activations = await _service(request, uow).activations(scope.project_id)
    return [ActivationOut.of(item) for item in activations]


@manifests_router.get("/manifests/{version}")
async def get_manifest(version: int, scope: ReadIntegrations, request: Request) -> ManifestOut:
    async with _uow(request) as uow:
        manifest = await _service(request, uow).get(scope.project_id, version)
    return ManifestOut.of(manifest)


@manifests_router.put("/manifests/{version}")
async def replace_manifest(
    version: int,
    body: ContentRequest,
    scope: ManageIntegrations,
    request: Request,
    audit: AuditCtx,
) -> ManifestOut:
    """Replace a draft's content. Validated versions are immutable: create a new version."""
    async with _uow(request) as uow:
        manifest = await _service(request, uow, audit).replace(scope, version, body.content)
    return ManifestOut.of(manifest)


@manifests_router.post("/manifests/{version}/validate")
async def validate_manifest(
    version: int, scope: ManageIntegrations, request: Request, audit: AuditCtx
) -> ManifestOut:
    """Check shape, cross-field rules and references (metrics, prices); freezes the content."""
    async with _uow(request) as uow:
        manifest = await _service(request, uow, audit).validate(scope, version)
    return ManifestOut.of(manifest)


@manifests_router.get("/manifests/{version}/diff")
async def diff_manifest(version: int, scope: ReadIntegrations, request: Request) -> dict[str, Any]:
    """Changes against the active version; ``money_sensitive`` marks split/price/wallet changes."""
    async with _uow(request) as uow:
        diff = await _service(request, uow).diff(scope.project_id, version)
    return diff.as_json()


@manifests_router.post("/manifests/{version}/approve")
async def approve_manifest(
    version: int,
    body: ApproveRequest,
    scope: ApproveIntegrations,
    request: Request,
    audit: AuditCtx,
) -> ManifestOut:
    """Approve exactly ``content_hash``. A money-sensitive change needs an operator other than
    its author (403 ``four-eyes-required``)."""
    async with _uow(request) as uow:
        manifest = await _service(request, uow, audit).approve(
            scope, version, content_hash=body.content_hash
        )
    return ManifestOut.of(manifest)


@manifests_router.post("/manifests/{version}/reject")
async def reject_manifest(
    version: int,
    body: RejectRequest,
    scope: ApproveIntegrations,
    request: Request,
    audit: AuditCtx,
) -> ManifestOut:
    async with _uow(request) as uow:
        manifest = await _service(request, uow, audit).reject(scope, version, reason=body.reason)
    return ManifestOut.of(manifest)


@manifests_router.post("/manifests/{version}/activate")
async def activate_manifest(
    version: int, scope: ApproveIntegrations, request: Request, audit: AuditCtx
) -> ActivateOut:
    """Activate an approved version, or roll back to a superseded one. Atomic: supersedes the
    active version and creates the declared metrics and webhook endpoints."""
    async with _uow(request) as uow:
        result = await _service(request, uow, audit).activate(scope, version)
    return ActivateOut.of(result)


@manifests_router.post("/manifests/{version}/simulate")
async def simulate_manifest(
    version: int, body: SimulateRequest, scope: ReadIntegrations, request: Request
) -> SimulationOut:
    """Dry run: the usage events, charge lines and split a business event would produce. Writes
    nothing."""
    async with _uow(request) as uow:
        result = await _service(request, uow).simulate(
            scope.project_id,
            version,
            name=body.name,
            payload=body.payload,
            idempotency_key=body.idempotency_key,
        )
    return SimulationOut.of(result)


@manifests_router.get("/business-events")
async def list_business_events(
    scope: ReadIntegrations, params: Annotated[PageParams, Query()], request: Request
) -> Page[BusinessEventOut]:
    async with _uow(request) as uow:
        page = await BusinessEventService(uow, clock=request.app.state.clock).list(
            scope.project_id, params
        )
    return Page[BusinessEventOut](
        data=[BusinessEventOut.of(item) for item in page.data],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


# -- ingestion (project API key) ---------------------------------------------------------------


def _ingestion_scope(principal: Principal) -> ProjectScope:
    if principal.kind != API_KEY_KIND or len(principal.project_ids) != 1:
        raise Forbidden("Business events are sent with a project API key.")
    (project_id,) = principal.project_ids
    scope = principal.scope_for(project_id)
    if scope is None or not scope.allows(Permission.EVENTS_WRITE):
        raise Forbidden(f"Missing permission {Permission.EVENTS_WRITE}.")
    return scope


@business_events_router.post(
    "",
    responses={
        status.HTTP_201_CREATED: {"model": BusinessEventOut, "description": "Recorded."},
        status.HTTP_409_CONFLICT: {
            "model": Problem,
            "description": "No active manifest, or the key was used with another payload.",
        },
    },
)
async def send_business_event(
    body: BusinessEventRequest, principal: Caller, request: Request, response: Response
) -> BusinessEventOut:
    """Record a business event; the active manifest maps it to usage. A retry answers 200."""
    scope = _ingestion_scope(principal)
    async with _uow(request) as uow:
        result = await BusinessEventService(uow, clock=request.app.state.clock).ingest(
            scope.project_id,
            name=body.name,
            payload=body.payload,
            idempotency_key=body.idempotency_key,
        )
    if result.created:
        response.status_code = status.HTTP_201_CREATED
    return BusinessEventOut.of(result.record)
