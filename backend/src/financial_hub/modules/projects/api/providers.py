"""Provider accounts of a project and the global provider catalog.

Credentials are accepted in request bodies (``SecretStr``: masked in any repr/log) and never
returned: responses carry ``has_credentials`` and, when safe, the last four characters. Needs
``integrations:manage`` to write and ``integrations:read`` to read; API keys hold neither.
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Request, status
from pydantic import BaseModel, Field, SecretStr

from financial_hub.modules.projects.application.providers import (
    CatalogEntry,
    ProviderAccountService,
    ProviderAccountView,
    provider_catalog,
)
from financial_hub.modules.projects.application.service import Permission
from financial_hub.platform.db import UnitOfWork
from financial_hub.platform.http.auth import Principal, ProjectScope, authenticated, require
from financial_hub.shared.pagination import Page, PageParams

router = APIRouter(prefix="/v1/projects/{project_id}/provider-accounts", tags=["providers"])
catalog_router = APIRouter(prefix="/v1/provider-catalog", tags=["providers"])


class CatalogEntryOut(BaseModel):
    provider: str
    enabled: bool
    environments: list[str]
    credential_fields: list[str]
    optional_credential_fields: list[str]

    @classmethod
    def of(cls, entry: CatalogEntry) -> CatalogEntryOut:
        return cls(
            provider=entry.provider,
            enabled=entry.enabled,
            environments=list(entry.environments),
            credential_fields=list(entry.credential_fields),
            optional_credential_fields=list(entry.optional_credential_fields),
        )


class ProviderAccountOut(BaseModel):
    id: str
    project_id: str
    provider: str
    environment: str
    name: str
    status: str
    has_credentials: bool
    credentials_last4: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, view: ProviderAccountView) -> ProviderAccountOut:
        return cls(
            id=view.id,
            project_id=view.project_id,
            provider=view.provider,
            environment=view.environment,
            name=view.name,
            status=view.status,
            has_credentials=view.has_credentials,
            credentials_last4=view.credentials_last4,
            created_at=view.created_at,
            updated_at=view.updated_at,
        )


class CreateProviderAccountRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=50)
    environment: str = Field(min_length=1, max_length=20)
    name: str = Field(min_length=1, max_length=100)
    credentials: dict[str, SecretStr] = Field(max_length=10)


class ReplaceCredentialsRequest(BaseModel):
    credentials: dict[str, SecretStr] = Field(max_length=10)


def _uow(request: Request) -> UnitOfWork:
    return UnitOfWork(request.app.state.session_factory)


def _service(request: Request, uow: UnitOfWork) -> ProviderAccountService:
    state = request.app.state
    return ProviderAccountService(uow, clock=state.clock, keyring=state.keyring)


def _plain(credentials: dict[str, SecretStr]) -> dict[str, str]:
    return {name: value.get_secret_value() for name, value in credentials.items()}


ReadAccounts = Annotated[ProjectScope, require(Permission.INTEGRATIONS_READ)]
ManageAccounts = Annotated[ProjectScope, require(Permission.INTEGRATIONS_MANAGE)]


@catalog_router.get("")
async def get_provider_catalog(
    _principal: Annotated[Principal, authenticated()],
) -> list[CatalogEntryOut]:
    return [CatalogEntryOut.of(entry) for entry in provider_catalog()]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_provider_account(
    body: CreateProviderAccountRequest, scope: ManageAccounts, request: Request
) -> ProviderAccountOut:
    async with _uow(request) as uow:
        view = await _service(request, uow).create(
            scope,
            provider=body.provider,
            environment=body.environment,
            name=body.name,
            credentials=_plain(body.credentials),
        )
    return ProviderAccountOut.of(view)


@router.get("")
async def list_provider_accounts(
    scope: ReadAccounts, params: Annotated[PageParams, Query()], request: Request
) -> Page[ProviderAccountOut]:
    async with _uow(request) as uow:
        page = await _service(request, uow).list_accounts(scope.project_id, params)
    return Page[ProviderAccountOut](
        data=[ProviderAccountOut.of(view) for view in page.data],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


@router.get("/{account_id}")
async def get_provider_account(
    account_id: str, scope: ReadAccounts, request: Request
) -> ProviderAccountOut:
    async with _uow(request) as uow:
        view = await _service(request, uow).get(scope.project_id, account_id)
    return ProviderAccountOut.of(view)


@router.put("/{account_id}/credentials")
async def replace_provider_credentials(
    account_id: str, body: ReplaceCredentialsRequest, scope: ManageAccounts, request: Request
) -> ProviderAccountOut:
    async with _uow(request) as uow:
        view = await _service(request, uow).replace_credentials(
            scope, account_id, _plain(body.credentials)
        )
    return ProviderAccountOut.of(view)


@router.post("/{account_id}/activate")
async def activate_provider_account(
    account_id: str, scope: ManageAccounts, request: Request
) -> ProviderAccountOut:
    async with _uow(request) as uow:
        view = await _service(request, uow).set_active(scope, account_id, active=True)
    return ProviderAccountOut.of(view)


@router.post("/{account_id}/deactivate")
async def deactivate_provider_account(
    account_id: str, scope: ManageAccounts, request: Request
) -> ProviderAccountOut:
    async with _uow(request) as uow:
        view = await _service(request, uow).set_active(scope, account_id, active=False)
    return ProviderAccountOut.of(view)
