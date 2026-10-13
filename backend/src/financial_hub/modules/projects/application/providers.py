"""Provider account use cases and the provider catalog.

Credentials enter through ``create``/``replace_credentials``, are validated, encrypted with
AES-256-GCM (``platform.crypto.seal``; AAD = ``project|provider|account``) and stored. Nothing in
this module can decrypt them (it does not import ``unseal``; import-linter contract 5), and no
view carries them: callers get ``has_credentials`` and, when safe, the last four characters.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

import structlog

from financial_hub.modules.projects.domain import (
    CATALOG,
    ProjectStatus,
    Provider,
    ProviderAccount,
    ProviderAccountStatus,
    ProviderEnvironment,
    ProviderNotEnabledError,
    credentials_aad,
    credentials_hint,
    encode_credentials,
    require_enabled,
    validate_account_name,
    validate_credentials,
)
from financial_hub.modules.projects.infrastructure import provider_repository, repository
from financial_hub.platform.crypto.keyring import Keyring
from financial_hub.platform.crypto.seal import SealedSecret, seal
from financial_hub.platform.db import UnitOfWork
from financial_hub.platform.http.auth import ProjectScope
from financial_hub.shared.clock import Clock
from financial_hub.shared.errors import (
    Conflict,
    FieldError,
    NotFound,
    ValidationFailed,
)
from financial_hub.shared.ids import IdPrefix, format_id, new_uuid, parse_id
from financial_hub.shared.pagination import Cursor, Page, PageParams, build_page

logger = structlog.get_logger()

ENVIRONMENTS: Final = tuple(environment.value for environment in ProviderEnvironment)


class ProviderNotEnabled(ValidationFailed):
    """The provider exists in the catalog but cannot be used yet (422)."""

    slug = "provider-not-enabled"
    title = "Provider not enabled"


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    provider: str
    enabled: bool
    environments: tuple[str, ...]
    credential_fields: tuple[str, ...]
    optional_credential_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProviderAccountView:
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


def provider_catalog() -> list[CatalogEntry]:
    return [
        CatalogEntry(
            provider=spec.provider.value,
            enabled=spec.enabled,
            environments=ENVIRONMENTS,
            credential_fields=spec.required,
            optional_credential_fields=spec.optional,
        )
        for spec in CATALOG.values()
    ]


def _view(account: ProviderAccount) -> ProviderAccountView:
    return ProviderAccountView(
        id=format_id(IdPrefix.PROVIDER_ACCOUNT, account.id),
        project_id=format_id(IdPrefix.PROJECT, account.project_id),
        provider=account.provider.value,
        environment=account.environment.value,
        name=account.name,
        status=account.status.value,
        has_credentials=True,  # the ciphertext column is NOT NULL
        credentials_last4=account.credentials_hint,
        created_at=account.created_at,
        updated_at=account.updated_at,
    )


def _invalid(pointer: str, error: ValueError) -> ValidationFailed:
    return ValidationFailed(
        "The request contains invalid fields.",
        errors=[FieldError(pointer=pointer, detail=str(error), code="invalid")],
    )


def _not_enabled(error: ProviderNotEnabledError) -> ProviderNotEnabled:
    return ProviderNotEnabled(
        f"Provider {error.provider.value!r} is not enabled yet.",
        errors=[FieldError(pointer="/provider", detail="not enabled", code="not_enabled")],
    )


def _provider(value: str) -> Provider:
    try:
        return Provider(value)
    except ValueError:
        raise _invalid("/provider", ValueError("unknown provider")) from None


def _environment(value: str) -> ProviderEnvironment:
    try:
        return ProviderEnvironment(value)
    except ValueError:
        raise _invalid("/environment", ValueError("must be sandbox or production")) from None


class ProviderAccountService:
    """Every method runs inside the caller's active :class:`UnitOfWork`."""

    def __init__(self, uow: UnitOfWork, *, clock: Clock, keyring: Keyring) -> None:
        self._uow = uow
        self._clock = clock
        self._keyring = keyring

    def _seal(
        self,
        project_id: UUID,
        provider: Provider,
        account_id: UUID,
        credentials: Mapping[str, str],
    ) -> SealedSecret:
        return seal(
            self._keyring,
            encode_credentials(credentials),
            aad=credentials_aad(project_id, provider, account_id),
        )

    async def create(
        self,
        scope: ProjectScope,
        *,
        provider: str,
        environment: str,
        name: str,
        credentials: Mapping[str, str],
    ) -> ProviderAccountView:
        chosen = _provider(provider)
        target = _environment(environment)
        try:
            require_enabled(chosen)
        except ProviderNotEnabledError as error:
            raise _not_enabled(error) from None
        try:
            name = validate_account_name(name)
        except ValueError as error:
            raise _invalid("/name", error) from None
        try:
            cleaned = validate_credentials(chosen, credentials)
        except ValueError as error:
            raise _invalid("/credentials", error) from None
        session = self._uow.session
        project = await repository.get_project(session, scope.project_id)
        if project is None:  # pragma: no cover - the scope proves the project exists
            raise NotFound("Project not found.")
        if project.status is not ProjectStatus.ACTIVE:
            raise Conflict("The project is archived.")
        account_id = new_uuid()
        account = await provider_repository.insert_account(
            session,
            account_id=account_id,
            project_id=scope.project_id,
            provider=chosen,
            environment=target,
            name=name,
            status=ProviderAccountStatus.ACTIVE,
            sealed=self._seal(scope.project_id, chosen, account_id, cleaned),
            credentials_hint=credentials_hint(chosen, cleaned),
            now=self._clock.now(),
        )
        if account is None:
            raise Conflict(
                "The project already has a provider account with this name.",
                errors=[FieldError(pointer="/name", detail="already taken", code="taken")],
            )
        logger.info(
            "provider_account.created",
            account_id=format_id(IdPrefix.PROVIDER_ACCOUNT, account.id),
            project_id=format_id(IdPrefix.PROJECT, account.project_id),
            provider=account.provider.value,
            environment=account.environment.value,
            created_by=scope.principal_id,
        )
        return _view(account)

    async def list_accounts(
        self, project_id: UUID, params: PageParams
    ) -> Page[ProviderAccountView]:
        rows = await provider_repository.list_accounts(
            self._uow.session, project_id, cursor=params.decoded_cursor(), limit=params.limit + 1
        )
        return build_page(
            rows, params.limit, _view, lambda row: Cursor(created_at=row.created_at, id=row.id)
        )

    async def _existing(self, project_id: UUID, account_id: str) -> ProviderAccount:
        try:
            parsed = parse_id(account_id, IdPrefix.PROVIDER_ACCOUNT)
        except ValueError:
            raise NotFound("Provider account not found.") from None
        account = await provider_repository.get_account(self._uow.session, project_id, parsed)
        if account is None:
            raise NotFound("Provider account not found.")
        return account

    async def get(self, project_id: UUID, account_id: str) -> ProviderAccountView:
        return _view(await self._existing(project_id, account_id))

    async def replace_credentials(
        self, scope: ProjectScope, account_id: str, credentials: Mapping[str, str]
    ) -> ProviderAccountView:
        account = await self._existing(scope.project_id, account_id)
        try:
            cleaned = validate_credentials(account.provider, credentials)
        except ValueError as error:
            raise _invalid("/credentials", error) from None
        updated = await provider_repository.replace_credentials(
            self._uow.session,
            scope.project_id,
            account.id,
            sealed=self._seal(scope.project_id, account.provider, account.id, cleaned),
            credentials_hint=credentials_hint(account.provider, cleaned),
            now=self._clock.now(),
        )
        if updated is None:  # pragma: no cover - the account was just read in this transaction
            raise NotFound("Provider account not found.")
        logger.info(
            "provider_account.credentials_replaced",
            account_id=format_id(IdPrefix.PROVIDER_ACCOUNT, updated.id),
            project_id=format_id(IdPrefix.PROJECT, updated.project_id),
            replaced_by=scope.principal_id,
        )
        return _view(updated)

    async def set_active(
        self, scope: ProjectScope, account_id: str, *, active: bool
    ) -> ProviderAccountView:
        """Activate (only enabled providers) or disable; both are idempotent."""
        account = await self._existing(scope.project_id, account_id)
        if active:
            try:
                require_enabled(account.provider)
            except ProviderNotEnabledError as error:
                raise _not_enabled(error) from None
        status = ProviderAccountStatus.ACTIVE if active else ProviderAccountStatus.DISABLED
        updated = await provider_repository.set_status(
            self._uow.session, scope.project_id, account.id, status, now=self._clock.now()
        )
        if updated is None:  # pragma: no cover - the account was just read in this transaction
            raise NotFound("Provider account not found.")
        logger.info(
            "provider_account.status_changed",
            account_id=format_id(IdPrefix.PROVIDER_ACCOUNT, updated.id),
            project_id=format_id(IdPrefix.PROJECT, updated.project_id),
            status=status.value,
            changed_by=scope.principal_id,
        )
        return _view(updated)
