"""API key persistence on the caller's session (runs as ``fh_app``: no DELETE, no secret reads).

Every lookup that is not by public prefix is keyed by ``(project_id, id)``, so a key id belonging
to another project is simply not found.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from financial_hub.modules.projects.domain import ApiKey, ApiKeyScope, KeyEnvironment, ProjectStatus
from financial_hub.modules.projects.infrastructure.tables import api_keys, projects
from financial_hub.shared.pagination import Cursor, keyset_predicate

_COLUMNS = (
    api_keys.c.id,
    api_keys.c.project_id,
    api_keys.c.name,
    api_keys.c.environment,
    api_keys.c.prefix,
    api_keys.c.scopes,
    api_keys.c.created_by,
    api_keys.c.created_at,
    api_keys.c.last_used_at,
    api_keys.c.expires_at,
    api_keys.c.revoked_at,
)


@dataclass(frozen=True, slots=True)
class StoredKey:
    """What authentication needs: the key, its digest and whether its project is active."""

    key: ApiKey
    secret_hash: bytes
    project_active: bool


def _environment(value: str) -> KeyEnvironment:
    return "live" if value == "live" else "test"


def _api_key(row: Any) -> ApiKey:
    return ApiKey(
        id=row.id,
        project_id=row.project_id,
        name=row.name,
        environment=_environment(row.environment),
        prefix=row.prefix,
        scopes=tuple(ApiKeyScope(scope) for scope in cast("list[str]", row.scopes)),
        created_by=row.created_by,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
    )


async def insert_api_key(
    session: AsyncSession,
    *,
    key_id: UUID,
    project_id: UUID,
    name: str,
    environment: KeyEnvironment,
    prefix: str,
    secret_hash: bytes,
    scopes: Sequence[ApiKeyScope],
    created_by: UUID,
    created_at: datetime,
    expires_at: datetime | None,
) -> ApiKey | None:
    """``None`` when the public prefix is already taken (nothing is written; mint another)."""
    statement = (
        insert(api_keys)
        .values(
            id=key_id,
            project_id=project_id,
            name=name,
            environment=environment,
            prefix=prefix,
            secret_hash=secret_hash,
            scopes=[scope.value for scope in scopes],
            created_by=created_by,
            created_at=created_at,
            expires_at=expires_at,
        )
        .on_conflict_do_nothing(index_elements=[api_keys.c.prefix])
        .returning(*_COLUMNS)
    )
    row = (await session.execute(statement)).first()
    return None if row is None else _api_key(row)


async def get_api_key(session: AsyncSession, project_id: UUID, key_id: UUID) -> ApiKey | None:
    statement = select(*_COLUMNS).where(
        api_keys.c.project_id == project_id, api_keys.c.id == key_id
    )
    row = (await session.execute(statement)).first()
    return None if row is None else _api_key(row)


async def list_api_keys(
    session: AsyncSession, project_id: UUID, *, cursor: Cursor | None, limit: int
) -> Sequence[ApiKey]:
    """Newest first, ``limit`` rows at most (callers pass ``limit + 1`` to detect more)."""
    statement = select(*_COLUMNS).where(api_keys.c.project_id == project_id)
    if cursor is not None:
        statement = statement.where(keyset_predicate(api_keys.c.created_at, api_keys.c.id, cursor))
    statement = statement.order_by(api_keys.c.created_at.desc(), api_keys.c.id.desc()).limit(limit)
    return [_api_key(row) for row in (await session.execute(statement)).all()]


async def revoke_api_key(
    session: AsyncSession, project_id: UUID, key_id: UUID, *, now: datetime
) -> ApiKey | None:
    """Revoke (idempotent: an already revoked key keeps its original ``revoked_at``)."""
    statement = (
        update(api_keys)
        .where(
            api_keys.c.project_id == project_id,
            api_keys.c.id == key_id,
            api_keys.c.revoked_at.is_(None),
        )
        .values(revoked_at=now)
        .returning(*_COLUMNS)
    )
    row = (await session.execute(statement)).first()
    if row is not None:
        return _api_key(row)
    return await get_api_key(session, project_id, key_id)


async def find_for_authentication(session: AsyncSession, prefix: str) -> StoredKey | None:
    statement = (
        select(*_COLUMNS, api_keys.c.secret_hash, projects.c.status.label("project_status"))
        .join(projects, projects.c.id == api_keys.c.project_id)
        .where(api_keys.c.prefix == prefix)
    )
    row = (await session.execute(statement)).first()
    if row is None:
        return None
    return StoredKey(
        key=_api_key(row),
        secret_hash=bytes(row.secret_hash),
        project_active=row.project_status == ProjectStatus.ACTIVE.value,
    )


async def touch_api_key(
    session: AsyncSession, key_id: UUID, *, now: datetime, min_interval: timedelta
) -> None:
    """Set ``last_used_at`` unless it was already written within ``min_interval`` (throttled)."""
    statement = (
        update(api_keys)
        .where(
            api_keys.c.id == key_id,
            (api_keys.c.last_used_at.is_(None)) | (api_keys.c.last_used_at <= now - min_interval),
        )
        .values(last_used_at=now)
    )
    await session.execute(statement)
