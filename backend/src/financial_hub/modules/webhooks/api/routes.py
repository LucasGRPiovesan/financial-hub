"""``/v1/projects/{project_id}/webhook-endpoints`` and webhook redelivery (docs/INTEGRATIONS.md s3).

Reading needs ``webhooks:read`` (owner, admin, finance); every change needs ``webhooks:manage``
(owner, admin). Both are operator-only: API keys can never hold them. The signing secret is in
the create and rotate responses only (``Cache-Control: no-store``, never replayed by idempotency).
"""

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import BaseModel, Field

from financial_hub.modules.audit.application import AuditContext, request_audit_context
from financial_hub.modules.identity.application.api import Permission
from financial_hub.modules.webhooks.application.service import (
    AttemptView,
    EndpointView,
    EndpointWithSecret,
    WebhookEndpointService,
)
from financial_hub.platform.db import UnitOfWork
from financial_hub.platform.http.auth import ProjectScope, require
from financial_hub.platform.http.protection.idempotency import idempotent
from financial_hub.shared.errors import NotFound
from financial_hub.shared.ids import IdPrefix, InvalidIdError, parse_id
from financial_hub.shared.pagination import Page, PageParams

router = APIRouter(prefix="/v1/projects/{project_id}/webhook-endpoints", tags=["webhooks"])
retry_router = APIRouter(prefix="/v1/projects/{project_id}/events", tags=["webhooks"])

ReadWebhooks = Annotated[ProjectScope, require(Permission.WEBHOOKS_READ)]
ManageWebhooks = Annotated[ProjectScope, require(Permission.WEBHOOKS_MANAGE)]
AuditCtx = Annotated[AuditContext, Depends(request_audit_context)]


class WebhookEndpointOut(BaseModel):
    id: str
    project_id: str
    url: str
    description: str | None
    event_types: list[str]
    enabled: bool
    secret_rotation_ends_at: datetime | None = Field(
        description="While set, deliveries carry a second signature made with the previous secret."
    )
    created_at: datetime
    updated_at: datetime
    disabled_at: datetime | None

    @classmethod
    def of(cls, view: EndpointView) -> WebhookEndpointOut:
        return cls(
            id=view.id,
            project_id=view.project_id,
            url=view.url,
            description=view.description,
            event_types=view.event_types,
            enabled=view.enabled,
            secret_rotation_ends_at=view.secret_rotation_ends_at,
            created_at=view.created_at,
            updated_at=view.updated_at,
            disabled_at=view.disabled_at,
        )


class WebhookEndpointWithSecretOut(WebhookEndpointOut):
    secret: str = Field(
        description="The signing secret (whsec_...). Shown only in this response; store it now."
    )

    @classmethod
    def created(cls, result: EndpointWithSecret) -> WebhookEndpointWithSecretOut:
        return cls(**WebhookEndpointOut.of(result.endpoint).model_dump(), secret=result.secret)

    def __repr__(self) -> str:
        return f"WebhookEndpointWithSecretOut(id={self.id!r}, secret=<hidden>)"


class CreateWebhookEndpointRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    description: str | None = Field(default=None, max_length=500)
    event_types: list[str] = Field(
        min_length=1,
        max_length=50,
        description='Event type filters: exact ("payment.succeeded"), prefix ("payment.*") or "*".',
    )


class UpdateWebhookEndpointRequest(BaseModel):
    url: str | None = Field(default=None, min_length=1, max_length=2048)
    description: str | None = Field(default=None, max_length=500)
    event_types: list[str] | None = Field(default=None, min_length=1, max_length=50)
    enabled: bool | None = None


class WebhookTestOut(BaseModel):
    event_id: str


class DeliveryAttemptOut(BaseModel):
    id: str
    delivery_id: str
    attempt: int
    outcome: str
    status_code: int | None
    latency_ms: int
    error: str | None
    attempted_at: datetime

    @classmethod
    def of(cls, view: AttemptView) -> DeliveryAttemptOut:
        return cls(
            id=str(view.id),
            delivery_id=view.delivery_id,
            attempt=view.attempt,
            outcome=view.outcome,
            status_code=view.status_code,
            latency_ms=view.latency_ms,
            error=view.error,
            attempted_at=view.attempted_at,
        )


class DeliveryAttemptsOut(BaseModel):
    data: list[DeliveryAttemptOut]


def _uow(request: Request) -> UnitOfWork:
    return UnitOfWork(request.app.state.session_factory)


def _service(
    request: Request, uow: UnitOfWork, audit: AuditContext | None = None
) -> WebhookEndpointService:
    state = request.app.state
    return WebhookEndpointService(
        uow,
        clock=state.clock,
        keyring=state.keyring,
        policy=state.webhook_policy,
        audit=audit,
    )


def _parse(value: str, prefix: IdPrefix, what: str) -> UUID:
    try:
        return parse_id(value, prefix)
    except InvalidIdError:
        raise NotFound(f"{what} not found.") from None


def _endpoint_id(value: str) -> UUID:
    return _parse(value, IdPrefix.WEBHOOK_ENDPOINT, "webhook endpoint")


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_webhook_endpoint(
    body: CreateWebhookEndpointRequest,
    scope: ManageWebhooks,
    request: Request,
    response: Response,
    audit: AuditCtx,
    # The secret is shown once: a replay never returns it (409 idempotency-response-unavailable).
    _idempotency: Annotated[None, idempotent(store_success_body=False)],
) -> WebhookEndpointWithSecretOut:
    async with _uow(request) as uow:
        created = await _service(request, uow, audit).create(
            scope, url=body.url, description=body.description, event_types=body.event_types
        )
    response.headers["Cache-Control"] = "no-store"
    return WebhookEndpointWithSecretOut.created(created)


@router.get("")
async def list_webhook_endpoints(
    scope: ReadWebhooks, params: Annotated[PageParams, Query()], request: Request
) -> Page[WebhookEndpointOut]:
    async with _uow(request) as uow:
        page = await _service(request, uow).list_endpoints(scope.project_id, params)
    return Page[WebhookEndpointOut](
        data=[WebhookEndpointOut.of(view) for view in page.data],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


@router.get("/{endpoint_id}")
async def get_webhook_endpoint(
    endpoint_id: str, scope: ReadWebhooks, request: Request
) -> WebhookEndpointOut:
    async with _uow(request) as uow:
        view = await _service(request, uow).get(scope.project_id, _endpoint_id(endpoint_id))
    return WebhookEndpointOut.of(view)


@router.patch("/{endpoint_id}")
async def update_webhook_endpoint(
    endpoint_id: str,
    body: UpdateWebhookEndpointRequest,
    scope: ManageWebhooks,
    request: Request,
    audit: AuditCtx,
) -> WebhookEndpointOut:
    changes: dict[str, Any] = {
        name: getattr(body, name) for name in body.model_fields_set if name != "description"
    }
    changes = {name: value for name, value in changes.items() if value is not None}
    if "description" in body.model_fields_set:
        changes["description"] = body.description  # null clears it
    async with _uow(request) as uow:
        view = await _service(request, uow, audit).update(
            scope, _endpoint_id(endpoint_id), **changes
        )
    return WebhookEndpointOut.of(view)


@router.post("/{endpoint_id}/disable")
async def disable_webhook_endpoint(
    endpoint_id: str, scope: ManageWebhooks, request: Request, audit: AuditCtx
) -> WebhookEndpointOut:
    async with _uow(request) as uow:
        view = await _service(request, uow, audit).disable(scope, _endpoint_id(endpoint_id))
    return WebhookEndpointOut.of(view)


@router.post("/{endpoint_id}/rotate-secret")
async def rotate_webhook_secret(
    endpoint_id: str,
    scope: ManageWebhooks,
    request: Request,
    response: Response,
    audit: AuditCtx,
    _idempotency: Annotated[None, idempotent(store_success_body=False)],
) -> WebhookEndpointWithSecretOut:
    async with _uow(request) as uow:
        rotated = await _service(request, uow, audit).rotate_secret(
            scope, _endpoint_id(endpoint_id)
        )
    response.headers["Cache-Control"] = "no-store"
    return WebhookEndpointWithSecretOut.created(rotated)


@router.post("/{endpoint_id}/test", status_code=status.HTTP_202_ACCEPTED)
async def send_webhook_test(
    endpoint_id: str, scope: ManageWebhooks, request: Request
) -> WebhookTestOut:
    async with _uow(request) as uow:
        event_id = await _service(request, uow).send_test(scope, _endpoint_id(endpoint_id))
    return WebhookTestOut(event_id=event_id)


@router.get("/{endpoint_id}/attempts")
async def list_webhook_attempts(
    endpoint_id: str,
    scope: ReadWebhooks,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> DeliveryAttemptsOut:
    async with _uow(request) as uow:
        attempts = await _service(request, uow).list_attempts(
            scope.project_id, _endpoint_id(endpoint_id), limit=limit
        )
    return DeliveryAttemptsOut(data=[DeliveryAttemptOut.of(item) for item in attempts])


@retry_router.post(
    "/{event_id}/deliveries/{delivery_id}/retry", status_code=status.HTTP_202_ACCEPTED
)
async def retry_webhook_delivery(
    event_id: str, delivery_id: str, scope: ManageWebhooks, request: Request
) -> Response:
    async with _uow(request) as uow:
        await _service(request, uow).retry_delivery(
            scope,
            event_id=_parse(event_id, IdPrefix.EVENT, "event"),
            delivery_id=_parse(delivery_id, IdPrefix.DELIVERY, "webhook delivery"),
        )
    return Response(status_code=status.HTTP_202_ACCEPTED)
