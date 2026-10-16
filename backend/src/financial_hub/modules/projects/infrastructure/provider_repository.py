"""Provider account persistence on the caller's session (runs as ``fh_app``: no DELETE).

Metadata reads never touch the ciphertext columns. The one function that reads them,
``load_sealed``, feeds ``credentials_vault`` (the only decrypting module).
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from financial_hub.modules.projects.domain import (
    Provider,
    ProviderAccount,
    ProviderAccountStatus,
    ProviderEnvironment,
)
from financial_hub.modules.projects.infrastructure.tables import provider_accounts
from financial_hub.platform.crypto.seal import SealedSecret
from financial_hub.shared.pagination import Cursor, keyset_predicate

_COLUMNS = (
    provider_accounts.c.id,
    provider_accounts.c.project_id,
    provider_accounts.c.provider,
    provider_accounts.c.environment,
    provider_accounts.c.name,
    provider_accounts.c.status,
    provider_accounts.c.key_version,
    provider_accounts.c.credentials_hint,
    provider_accounts.c.created_at,
    provider_accounts.c.updated_at,
)


def _account(row: Any) -> ProviderAccount:
    return ProviderAccount(
        id=row.id,
        project_id=row.project_id,
        provider=Provider(row.provider),
        environment=ProviderEnvironment(row.environment),
        name=row.name,
        status=ProviderAccountStatus(row.status),
        key_version=row.key_version,
        credentials_hint=row.credentials_hint,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def insert_account(
    session: AsyncSession,
    *,
    account_id: UUID,
    project_id: UUID,
    provider: Provider,
    environment: ProviderEnvironment,
    name: str,
    status: ProviderAccountStatus,
    sealed: SealedSecret,
    credentials_hint: str | None,
    now: datetime,
) -> ProviderAccount | None:
    """``None`` when the project already has an account with this provider/environment/name."""
    statement = (
        insert(provider_accounts)
        .values(
            id=account_id,
            project_id=project_id,
            provider=provider.value,
            environment=environment.value,
            name=name,
            status=status.value,
            key_version=sealed.key_version,
            nonce=sealed.nonce,
            ciphertext=sealed.ciphertext,
            credentials_hint=credentials_hint,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(
            index_elements=[
                provider_accounts.c.project_id,
                provider_accounts.c.provider,
                provider_accounts.c.environment,
                provider_accounts.c.name,
            ]
        )
        .returning(*_COLUMNS)
    )
    row = (await session.execute(statement)).first()
    return None if row is None else _account(row)


async def get_account(
    session: AsyncSession, project_id: UUID, account_id: UUID
) -> ProviderAccount | None:
    statement = select(*_COLUMNS).where(
        provider_accounts.c.project_id == project_id, provider_accounts.c.id == account_id
    )
    row = (await session.execute(statement)).first()
    return None if row is None else _account(row)


async def list_accounts(
    session: AsyncSession, project_id: UUID, *, cursor: Cursor | None, limit: int
) -> Sequence[ProviderAccount]:
    """Newest first, ``limit`` rows at most (callers pass ``limit + 1`` to detect more)."""
    statement = select(*_COLUMNS).where(provider_accounts.c.project_id == project_id)
    if cursor is not None:
        statement = statement.where(
            keyset_predicate(provider_accounts.c.created_at, provider_accounts.c.id, cursor)
        )
    statement = statement.order_by(
        provider_accounts.c.created_at.desc(), provider_accounts.c.id.desc()
    ).limit(limit)
    return [_account(row) for row in (await session.execute(statement)).all()]


async def set_status(
    session: AsyncSession,
    project_id: UUID,
    account_id: UUID,
    status: ProviderAccountStatus,
    *,
    now: datetime,
) -> ProviderAccount | None:
    statement = (
        update(provider_accounts)
        .where(provider_accounts.c.project_id == project_id, provider_accounts.c.id == account_id)
        .values(status=status.value, updated_at=now)
        .returning(*_COLUMNS)
    )
    row = (await session.execute(statement)).first()
    return None if row is None else _account(row)


async def replace_credentials(
    session: AsyncSession,
    project_id: UUID,
    account_id: UUID,
    *,
    sealed: SealedSecret,
    credentials_hint: str | None,
    now: datetime,
) -> ProviderAccount | None:
    statement = (
        update(provider_accounts)
        .where(provider_accounts.c.project_id == project_id, provider_accounts.c.id == account_id)
        .values(
            key_version=sealed.key_version,
            nonce=sealed.nonce,
            ciphertext=sealed.ciphertext,
            credentials_hint=credentials_hint,
            updated_at=now,
        )
        .returning(*_COLUMNS)
    )
    row = (await session.execute(statement)).first()
    return None if row is None else _account(row)


async def load_sealed(
    session: AsyncSession, project_id: UUID, account_id: UUID
) -> tuple[ProviderAccount, SealedSecret] | None:
    """The account with its encrypted credentials. Only ``credentials_vault`` should call this."""
    statement = select(*_COLUMNS, provider_accounts.c.nonce, provider_accounts.c.ciphertext).where(
        provider_accounts.c.project_id == project_id, provider_accounts.c.id == account_id
    )
    row = (await session.execute(statement)).first()
    if row is None:
        return None
    sealed = SealedSecret(
        key_version=row.key_version, nonce=bytes(row.nonce), ciphertext=bytes(row.ciphertext)
    )
    return _account(row), sealed


async def list_active_accounts(
    session: AsyncSession, project_id: UUID
) -> Sequence[ProviderAccount]:
    """The project's active accounts, oldest first (few per project: no pagination)."""
    statement = (
        select(*_COLUMNS)
        .where(
            provider_accounts.c.project_id == project_id,
            provider_accounts.c.status == ProviderAccountStatus.ACTIVE.value,
        )
        .order_by(provider_accounts.c.created_at, provider_accounts.c.id)
    )
    return [_account(row) for row in (await session.execute(statement)).all()]
