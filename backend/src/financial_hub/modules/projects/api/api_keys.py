"""``/v1/projects/{project_id}/api-keys``: create (secret shown once), list, revoke.

Operators with ``api_keys:manage`` only: API keys can never hold that permission, so a key cannot
mint, list or revoke keys. Every handler runs one Unit of Work.
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import BaseModel, Field

from financial_hub.modules.audit.application import AuditContext, request_audit_context
from financial_hub.modules.projects.application.api_keys import (
    ApiKeyService,
    ApiKeyView,
    CreatedApiKey,
)
from financial_hub.modules.projects.application.service import Permission
from financial_hub.platform.db import UnitOfWork
from financial_hub.platform.http.auth import ProjectScope, require
from financial_hub.platform.http.protection.idempotency import idempotent
from financial_hub.shared.pagination import Page, PageParams

router = APIRouter(prefix="/v1/projects/{project_id}/api-keys", tags=["api-keys"])


class ApiKeyOut(BaseModel):
    id: str
    project_id: str
    name: str
    environment: str
    prefix: str
    scopes: list[str]
    status: str
    created_by: str
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None

    @classmethod
    def of(cls, view: ApiKeyView) -> ApiKeyOut:
        return cls(
            id=view.id,
            project_id=view.project_id,
            name=view.name,
            environment=view.environment,
            prefix=view.prefix,
            scopes=list(view.scopes),
            status=view.status,
            created_by=view.created_by,
            created_at=view.created_at,
            last_used_at=view.last_used_at,
            expires_at=view.expires_at,
            revoked_at=view.revoked_at,
        )


class CreatedApiKeyOut(ApiKeyOut):
    key: str = Field(description="The full API key. Shown only in this response; store it now.")

    @classmethod
    def created(cls, created: CreatedApiKey) -> CreatedApiKeyOut:
        return cls(**ApiKeyOut.of(created.view).model_dump(), key=created.key)

    def __repr__(self) -> str:
        return f"CreatedApiKeyOut(id={self.id!r}, key=<hidden>)"


class CreateApiKeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scopes: list[str] = Field(min_length=1, max_length=20)
    expires_at: datetime | None = None


def _service(request: Request, uow: UnitOfWork, audit: AuditContext | None = None) -> ApiKeyService:
    state = request.app.state
    return ApiKeyService(
        uow,
        clock=state.clock,
        pepper=state.api_key_pepper,
        environment=state.api_key_environment,
        audit=audit,
    )


def _uow(request: Request) -> UnitOfWork:
    return UnitOfWork(request.app.state.session_factory)


ManageKeys = Annotated[ProjectScope, require(Permission.API_KEYS_MANAGE)]
AuditCtx = Annotated[AuditContext, Depends(request_audit_context)]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_api_key(
    body: CreateApiKeyRequest,
    scope: ManageKeys,
    request: Request,
    response: Response,
    audit: AuditCtx,
    # The secret is shown once: a replay never returns it (409 idempotency-response-unavailable).
    _idempotency: Annotated[None, idempotent(store_success_body=False)],
) -> CreatedApiKeyOut:
    async with _uow(request) as uow:
        created = await _service(request, uow, audit).create(
            scope, name=body.name, scopes=body.scopes, expires_at=body.expires_at
        )
    response.headers["Cache-Control"] = "no-store"
    return CreatedApiKeyOut.created(created)


@router.get("")
async def list_api_keys(
    scope: ManageKeys, params: Annotated[PageParams, Query()], request: Request
) -> Page[ApiKeyOut]:
    async with _uow(request) as uow:
        page = await _service(request, uow).list_keys(scope.project_id, params)
    return Page[ApiKeyOut](
        data=[ApiKeyOut.of(view) for view in page.data],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


@router.post("/{key_id}/revoke")
async def revoke_api_key(
    key_id: str, scope: ManageKeys, request: Request, audit: AuditCtx
) -> ApiKeyOut:
    async with _uow(request) as uow:
        view = await _service(request, uow, audit).revoke(scope, key_id)
    return ApiKeyOut.of(view)
