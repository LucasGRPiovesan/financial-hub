"""Webhook endpoint persistence: plain SQL on the caller's session (runs as ``fh_app``: no DELETE).

Metadata reads never select the ciphertext columns. The one function that reads them,
``load_sealed_secrets``, feeds ``secret_vault`` (the only decrypting module of webhooks).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import RowMapping, TextClause, text
from sqlalchemy.ext.asyncio import AsyncSession

from financial_hub.modules.webhooks.domain import WebhookEndpoint
from financial_hub.platform.crypto.seal import SealedSecret
from financial_hub.shared.pagination import Cursor

_COLUMNS = """
    id, project_id, subscription_id, url, description, event_types, enabled, secret_key_version,
    previous_expires_at, created_at, updated_at, disabled_at
"""


def _returning(statement: str) -> TextClause:
    """``statement RETURNING <endpoint columns>`` (fixed fragments only, no user input)."""
    return text(f"{statement} RETURNING {_COLUMNS}")


_INSERT = _returning(
    """
    INSERT INTO public.webhook_endpoints (id, project_id, subscription_id, url, description,
        event_types, enabled, secret_key_version, secret_nonce, secret_ciphertext, created_at,
        updated_at)
    VALUES (:id, :project_id, :subscription_id, :url, :description,
        CAST(:event_types AS text[]), true, :key_version, :nonce, :ciphertext, :now, :now)
    """
)
_GET = text(
    f"SELECT {_COLUMNS} FROM public.webhook_endpoints "  # noqa: S608 - fixed fragment
    "WHERE id = :id AND project_id = :project_id"
)
_GET_FOR_UPDATE = text(
    f"SELECT {_COLUMNS} FROM public.webhook_endpoints "  # noqa: S608 - fixed fragment
    "WHERE id = :id AND project_id = :project_id FOR UPDATE"
)
_UPDATE = _returning(
    """
    UPDATE public.webhook_endpoints
    SET url = :url, description = :description, event_types = CAST(:event_types AS text[]),
        enabled = :enabled, disabled_at = :disabled_at, updated_at = :now
    WHERE id = :id
    """
)
_ROTATE = _returning(
    """
    UPDATE public.webhook_endpoints
    SET previous_key_version = secret_key_version, previous_nonce = secret_nonce,
        previous_ciphertext = secret_ciphertext, previous_expires_at = :previous_expires_at,
        secret_key_version = :key_version, secret_nonce = :nonce, secret_ciphertext = :ciphertext,
        updated_at = :now
    WHERE id = :id
    """
)
_LOAD_SEALED = text(
    """
    SELECT secret_key_version, secret_nonce, secret_ciphertext, previous_key_version,
           previous_nonce, previous_ciphertext, previous_expires_at
    FROM public.webhook_endpoints WHERE id = :id
    """
)
_SENDING_TARGET = text(
    "SELECT id, project_id, url, enabled FROM public.webhook_endpoints WHERE id = :id"
)
_INSERT_ATTEMPT = text(
    """
    INSERT INTO public.webhook_delivery_attempts (id, project_id, endpoint_id, delivery_id,
        attempt, outcome, status_code, latency_ms, error, attempted_at)
    VALUES (:id, :project_id, :endpoint_id, :delivery_id, :attempt, :outcome, :status_code,
        :latency_ms, :error, :attempted_at)
    """
)


@dataclass(frozen=True, slots=True)
class SealedSecrets:
    current: SealedSecret
    previous: SealedSecret | None
    previous_expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class SendingTarget:
    id: UUID
    project_id: UUID
    url: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    id: UUID
    delivery_id: UUID
    attempt: int
    outcome: str
    status_code: int | None
    latency_ms: int
    error: str | None
    attempted_at: datetime


def _endpoint(row: RowMapping) -> WebhookEndpoint:
    return WebhookEndpoint(
        id=row["id"],
        project_id=row["project_id"],
        subscription_id=row["subscription_id"],
        url=row["url"],
        description=row["description"],
        event_types=tuple(row["event_types"]),
        enabled=row["enabled"],
        secret_key_version=row["secret_key_version"],
        previous_expires_at=row["previous_expires_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        disabled_at=row["disabled_at"],
    )


async def insert_endpoint(
    session: AsyncSession,
    *,
    endpoint_id: UUID,
    project_id: UUID,
    subscription_id: UUID,
    url: str,
    description: str | None,
    event_types: Sequence[str],
    sealed: SealedSecret,
    now: datetime,
) -> WebhookEndpoint:
    result = await session.execute(
        _INSERT,
        {
            "id": endpoint_id,
            "project_id": project_id,
            "subscription_id": subscription_id,
            "url": url,
            "description": description,
            "event_types": list(event_types),
            "key_version": sealed.key_version,
            "nonce": sealed.nonce,
            "ciphertext": sealed.ciphertext,
            "now": now,
        },
    )
    return _endpoint(result.mappings().one())


async def get_endpoint(
    session: AsyncSession, project_id: UUID, endpoint_id: UUID, *, for_update: bool = False
) -> WebhookEndpoint | None:
    statement = _GET_FOR_UPDATE if for_update else _GET
    result = await session.execute(statement, {"id": endpoint_id, "project_id": project_id})
    row = result.mappings().one_or_none()
    return None if row is None else _endpoint(row)


async def list_endpoints(
    session: AsyncSession, project_id: UUID, *, cursor: Cursor | None, limit: int
) -> list[WebhookEndpoint]:
    params: dict[str, Any] = {"project_id": project_id, "limit": limit + 1}
    where = "project_id = :project_id"
    if cursor is not None:
        where += " AND (created_at, id) < (:cursor_created_at, :cursor_id)"
        params |= {"cursor_created_at": cursor.created_at, "cursor_id": cursor.id}
    statement = text(
        f"SELECT {_COLUMNS} FROM public.webhook_endpoints WHERE {where} "  # noqa: S608
        "ORDER BY created_at DESC, id DESC LIMIT :limit"
    )
    result = await session.execute(statement, params)
    return [_endpoint(row) for row in result.mappings()]


async def update_endpoint(
    session: AsyncSession,
    endpoint: WebhookEndpoint,
    *,
    url: str,
    description: str | None,
    event_types: Sequence[str],
    enabled: bool,
    now: datetime,
) -> WebhookEndpoint:
    disabled_at = None if enabled else (endpoint.disabled_at or now)
    result = await session.execute(
        _UPDATE,
        {
            "id": endpoint.id,
            "url": url,
            "description": description,
            "event_types": list(event_types),
            "enabled": enabled,
            "disabled_at": disabled_at,
            "now": now,
        },
    )
    return _endpoint(result.mappings().one())


async def rotate_secret(
    session: AsyncSession,
    endpoint_id: UUID,
    *,
    sealed: SealedSecret,
    previous_expires_at: datetime,
    now: datetime,
) -> WebhookEndpoint:
    result = await session.execute(
        _ROTATE,
        {
            "id": endpoint_id,
            "key_version": sealed.key_version,
            "nonce": sealed.nonce,
            "ciphertext": sealed.ciphertext,
            "previous_expires_at": previous_expires_at,
            "now": now,
        },
    )
    return _endpoint(result.mappings().one())


async def load_sealed_secrets(session: AsyncSession, endpoint_id: UUID) -> SealedSecrets | None:
    row = (await session.execute(_LOAD_SEALED, {"id": endpoint_id})).mappings().one_or_none()
    if row is None:
        return None
    previous = (
        None
        if row["previous_key_version"] is None
        else SealedSecret(
            row["previous_key_version"], row["previous_nonce"], row["previous_ciphertext"]
        )
    )
    return SealedSecrets(
        current=SealedSecret(
            row["secret_key_version"], row["secret_nonce"], row["secret_ciphertext"]
        ),
        previous=previous,
        previous_expires_at=row["previous_expires_at"],
    )


async def sending_target(session: AsyncSession, endpoint_id: UUID) -> SendingTarget | None:
    row = (await session.execute(_SENDING_TARGET, {"id": endpoint_id})).mappings().one_or_none()
    if row is None:
        return None
    return SendingTarget(row["id"], row["project_id"], row["url"], row["enabled"])


async def insert_attempt(
    session: AsyncSession,
    *,
    attempt_id: UUID,
    project_id: UUID,
    endpoint_id: UUID,
    delivery_id: UUID,
    attempt: int,
    outcome: str,
    status_code: int | None,
    latency_ms: int,
    error: str | None,
    attempted_at: datetime,
) -> None:
    await session.execute(
        _INSERT_ATTEMPT,
        {
            "id": attempt_id,
            "project_id": project_id,
            "endpoint_id": endpoint_id,
            "delivery_id": delivery_id,
            "attempt": attempt,
            "outcome": outcome,
            "status_code": status_code,
            "latency_ms": latency_ms,
            "error": error,
            "attempted_at": attempted_at,
        },
    )


async def list_attempts(
    session: AsyncSession, project_id: UUID, endpoint_id: UUID, *, limit: int
) -> list[AttemptRecord]:
    result = await session.execute(
        text(
            """
            SELECT id, delivery_id, attempt, outcome, status_code, latency_ms, error, attempted_at
            FROM public.webhook_delivery_attempts
            WHERE project_id = :project_id AND endpoint_id = :endpoint_id
            ORDER BY attempted_at DESC, id DESC
            LIMIT :limit
            """
        ),
        {"project_id": project_id, "endpoint_id": endpoint_id, "limit": limit},
    )
    return [
        AttemptRecord(
            id=row["id"],
            delivery_id=row["delivery_id"],
            attempt=row["attempt"],
            outcome=row["outcome"],
            status_code=row["status_code"],
            latency_ms=row["latency_ms"],
            error=row["error"],
            attempted_at=row["attempted_at"],
        )
        for row in result.mappings()
    ]
