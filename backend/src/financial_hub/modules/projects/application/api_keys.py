"""API key use cases: create (secret shown once), list, revoke, and bearer authentication.

Authentication failures are indistinguishable on purpose (unknown prefix, wrong secret, revoked,
expired, wrong environment, archived project): one ``Unauthorized`` with one message, after the
same HMAC work, so a caller learns nothing about which keys exist.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from financial_hub.modules.audit.application import (
    Actor,
    AuditContext,
    AuditRecorder,
    AuditTarget,
    changed_fields,
)
from financial_hub.modules.projects.application.scopes import permissions_for_scopes
from financial_hub.modules.projects.domain import (
    DUMMY_DIGEST,
    ApiKey,
    KeyEnvironment,
    ProjectStatus,
    hash_secret,
    mint_key,
    parse_key,
    secret_matches,
    validate_key_name,
    validate_scopes,
)
from financial_hub.modules.projects.infrastructure import api_key_repository, repository
from financial_hub.platform.db import UnitOfWork
from financial_hub.platform.http.auth import Principal, ProjectScope
from financial_hub.shared.clock import Clock
from financial_hub.shared.errors import (
    Conflict,
    FieldError,
    Forbidden,
    NotFound,
    Unauthorized,
    ValidationFailed,
)
from financial_hub.shared.ids import IdPrefix, format_id, new_uuid, parse_id
from financial_hub.shared.pagination import Cursor, Page, PageParams, build_page

logger = structlog.get_logger()

API_KEY_PRINCIPAL: Final = "api_key"
BEARER_KEY_PREFIX: Final = "fh_"
OPERATOR_PRINCIPAL: Final = "operator"
MAX_PREFIX_ATTEMPTS: Final = 5
INVALID_KEY: Final = "Invalid or expired API key."


@dataclass(frozen=True, slots=True)
class ApiKeyPrincipal:
    """A project API key (implements ``Principal``): sees exactly one project."""

    key_id: UUID
    project_id: UUID
    permissions: frozenset[str]

    @property
    def kind(self) -> str:
        return API_KEY_PRINCIPAL

    @property
    def subject_id(self) -> str:
        return format_id(IdPrefix.API_KEY, self.key_id)

    @property
    def project_ids(self) -> frozenset[UUID]:
        return frozenset({self.project_id})

    def scope_for(self, project_id: UUID) -> ProjectScope | None:
        if project_id != self.project_id:
            return None
        return ProjectScope(
            project_id=project_id,
            principal_kind=API_KEY_PRINCIPAL,
            principal_id=self.subject_id,
            permissions=self.permissions,
        )


@dataclass(frozen=True, slots=True)
class ApiKeyView:
    id: str
    project_id: str
    name: str
    environment: str
    prefix: str
    scopes: tuple[str, ...]
    status: str
    created_by: str
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None


@dataclass(frozen=True, slots=True)
class CreatedApiKey:
    """The create response: the one and only time the full key is available."""

    view: ApiKeyView
    key: str

    def __repr__(self) -> str:
        return f"CreatedApiKey(view={self.view!r}, key=<hidden>)"


def _status(key: ApiKey, now: datetime) -> str:
    if key.revoked_at is not None:
        return "revoked"
    if key.expires_at is not None and key.expires_at <= now:
        return "expired"
    return "active"


def _view(key: ApiKey, now: datetime) -> ApiKeyView:
    return ApiKeyView(
        id=format_id(IdPrefix.API_KEY, key.id),
        project_id=format_id(IdPrefix.PROJECT, key.project_id),
        name=key.name,
        environment=key.environment,
        prefix=key.prefix,
        scopes=tuple(scope.value for scope in key.scopes),
        status=_status(key, now),
        created_by=format_id(IdPrefix.OPERATOR, key.created_by),
        created_at=key.created_at,
        last_used_at=key.last_used_at,
        expires_at=key.expires_at,
        revoked_at=key.revoked_at,
    )


def _invalid(pointer: str, error: ValueError) -> ValidationFailed:
    return ValidationFailed(
        "The request contains invalid fields.",
        errors=[FieldError(pointer=pointer, detail=str(error), code="invalid")],
    )


class ApiKeyService:
    """Management use cases; every method runs inside the caller's active :class:`UnitOfWork`."""

    def __init__(
        self,
        uow: UnitOfWork,
        *,
        clock: Clock,
        pepper: bytes,
        environment: KeyEnvironment,
        audit: AuditContext | None = None,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._pepper = pepper
        self._environment: KeyEnvironment = environment
        self._audit = audit or AuditContext.system()

    async def create(
        self,
        scope: ProjectScope,
        *,
        name: str,
        scopes: list[str],
        expires_at: datetime | None,
    ) -> CreatedApiKey:
        if scope.principal_kind != OPERATOR_PRINCIPAL:
            raise Forbidden("Only operators can create API keys.")
        now = self._clock.now()
        try:
            name = validate_key_name(name)
        except ValueError as error:
            raise _invalid("/name", error) from None
        try:
            granted = validate_scopes(scopes)
        except ValueError as error:
            raise _invalid("/scopes", error) from None
        if expires_at is not None and (expires_at.utcoffset() is None or expires_at <= now):
            raise _invalid("/expires_at", ValueError("expires_at must be a future timestamp"))
        session = self._uow.session
        project = await repository.get_project(session, scope.project_id)
        if project is None:  # pragma: no cover - the scope proves the project exists
            raise NotFound("Project not found.")
        if project.status is not ProjectStatus.ACTIVE:
            raise Conflict("The project is archived.")
        created_by = parse_id(scope.principal_id, IdPrefix.OPERATOR)
        for _ in range(MAX_PREFIX_ATTEMPTS):
            minted = mint_key(self._environment)
            stored = await api_key_repository.insert_api_key(
                session,
                key_id=new_uuid(),
                project_id=scope.project_id,
                name=name,
                environment=minted.environment,
                prefix=minted.prefix,
                secret_hash=hash_secret(self._pepper, minted.secret),
                scopes=granted,
                created_by=created_by,
                created_at=now,
                expires_at=expires_at,
            )
            if stored is not None:
                await self._record(
                    scope,
                    "api_key.created",
                    stored,
                    changed_fields(
                        None,
                        {
                            "name": stored.name,
                            "prefix": stored.prefix,
                            "environment": stored.environment,
                            "scopes": [item.value for item in stored.scopes],
                            "expires_at": stored.expires_at,
                        },
                    ),
                )
                logger.info(
                    "api_key.created",
                    key_id=format_id(IdPrefix.API_KEY, stored.id),
                    project_id=format_id(IdPrefix.PROJECT, stored.project_id),
                    prefix=stored.prefix,
                    created_by=scope.principal_id,
                )
                return CreatedApiKey(view=_view(stored, now), key=minted.plaintext)
        raise RuntimeError("could not allocate a unique API key prefix")  # pragma: no cover

    async def _record(
        self, scope: ProjectScope, action: str, key: ApiKey, diff: dict[str, Any]
    ) -> None:
        await AuditRecorder.record(
            self._uow.session,
            actor=self._audit.acting(Actor.of_principal(scope.principal_kind, scope.principal_id)),
            action=action,
            target=AuditTarget("api_key", format_id(IdPrefix.API_KEY, key.id)),
            diff=diff,
            project_id=key.project_id,
        )

    async def list_keys(self, project_id: UUID, params: PageParams) -> Page[ApiKeyView]:
        now = self._clock.now()
        rows = await api_key_repository.list_api_keys(
            self._uow.session, project_id, cursor=params.decoded_cursor(), limit=params.limit + 1
        )
        return build_page(
            rows,
            params.limit,
            lambda row: _view(row, now),
            lambda row: Cursor(created_at=row.created_at, id=row.id),
        )

    async def revoke(self, scope: ProjectScope, key_id: str) -> ApiKeyView:
        """Revoke a key of this project (idempotent). A key of another project is a 404."""
        now = self._clock.now()
        try:
            key_uuid = parse_id(key_id, IdPrefix.API_KEY)
        except ValueError:
            raise NotFound("API key not found.") from None
        before = await api_key_repository.get_api_key(self._uow.session, scope.project_id, key_uuid)
        key = await api_key_repository.revoke_api_key(
            self._uow.session, scope.project_id, key_uuid, now=now
        )
        if key is None or before is None:
            raise NotFound("API key not found.")
        if before.revoked_at is None and key.revoked_at == now:
            await self._record(
                scope,
                "api_key.revoked",
                key,
                changed_fields(
                    {"name": key.name, "prefix": key.prefix, "revoked_at": None},
                    {"name": key.name, "prefix": key.prefix, "revoked_at": key.revoked_at},
                ),
            )
        logger.info(
            "api_key.revoked",
            key_id=format_id(IdPrefix.API_KEY, key.id),
            project_id=format_id(IdPrefix.PROJECT, key.project_id),
            revoked_by=scope.principal_id,
        )
        return _view(key, now)


class ApiKeyAuthService:
    """Bearer authentication; opens its own short transaction like the session authenticator."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Clock,
        pepper: bytes,
        environment: KeyEnvironment,
        touch_interval: timedelta,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._pepper = pepper
        self._environment: KeyEnvironment = environment
        self._touch_interval = touch_interval

    async def authenticate(self, token: str) -> Principal:
        """The principal behind ``token``; ``Unauthorized`` (always the same) otherwise."""
        parsed = parse_key(token)
        if parsed is None:
            hash_secret(self._pepper, "x")  # keep the cost of a malformed key close to a miss
            raise Unauthorized(INVALID_KEY)
        now = self._clock.now()
        async with UnitOfWork(self._session_factory) as uow:
            stored = await api_key_repository.find_for_authentication(uow.session, parsed.prefix)
            digest = DUMMY_DIGEST if stored is None else stored.secret_hash
            matches = secret_matches(self._pepper, parsed.secret, digest)
            if (
                stored is None
                or not matches
                or stored.key.environment != parsed.environment
                or stored.key.environment != self._environment
                or not stored.key.is_usable(now)
                or not stored.project_active
            ):
                raise Unauthorized(INVALID_KEY)
            await api_key_repository.touch_api_key(
                uow.session, stored.key.id, now=now, min_interval=self._touch_interval
            )
        return ApiKeyPrincipal(
            key_id=stored.key.id,
            project_id=stored.key.project_id,
            permissions=permissions_for_scopes(stored.key.scopes),
        )
