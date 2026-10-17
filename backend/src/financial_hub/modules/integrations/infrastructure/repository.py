"""Integrations persistence on the caller's session (runs as ``fh_app``).

Every read is scoped by ``project_id``. Activations, materialized endpoints and business events are
append-only (INSERT and SELECT only); manifest rows change only through their lifecycle columns
(the ``0015`` trigger refuses anything else). Writes that pick a version number or the active
version first take :func:`lock_project`, a transaction-scoped advisory lock per project.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from financial_hub.modules.integrations.domain import (
    Activation,
    BusinessEventRecord,
    ManifestStatus,
    ManifestVersion,
)
from financial_hub.modules.integrations.infrastructure.tables import (
    business_events,
    integration_manifest_activations,
    integration_manifests,
    integration_webhook_endpoints,
)
from financial_hub.shared.pagination import Cursor, keyset_predicate

_manifest = integration_manifests
_activation = integration_manifest_activations
_endpoint = integration_webhook_endpoints
_event = business_events


def _to_manifest(row: Any) -> ManifestVersion:
    return ManifestVersion(
        id=row.id,
        project_id=row.project_id,
        version=row.version,
        status=ManifestStatus(row.status),
        content=dict(row.content),
        content_hash=row.content_hash,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
        validated_at=row.validated_at,
        approved_by=row.approved_by,
        approved_at=row.approved_at,
        four_eyes=row.four_eyes,
        approval_base_version=row.approval_base_version,
        approval_diff=None if row.approval_diff is None else dict(row.approval_diff),
        rejected_by=row.rejected_by,
        rejected_at=row.rejected_at,
        rejection_reason=row.rejection_reason,
        activated_at=row.activated_at,
        superseded_at=row.superseded_at,
    )


def _to_activation(row: Any) -> Activation:
    return Activation(
        id=row.id,
        project_id=row.project_id,
        version=row.version,
        previous_version=row.previous_version,
        activated_by=row.activated_by,
        activated_at=row.activated_at,
        materialized=dict(row.materialized),
    )


def _to_event(row: Any) -> BusinessEventRecord:
    return BusinessEventRecord(
        id=row.id,
        project_id=row.project_id,
        name=row.name,
        idempotency_key=row.idempotency_key,
        payload_hash=row.payload_hash,
        payload=dict(row.payload),
        manifest_version=row.manifest_version,
        outcome=dict(row.outcome),
        received_at=row.received_at,
    )


async def lock_project(session: AsyncSession, project_id: UUID) -> None:
    """Serialize version numbering and activation of one project until the transaction ends."""
    key = func.hashtextextended(f"integration_manifests:{project_id}", 0)
    await session.execute(select(func.pg_advisory_xact_lock(key)))


# -- manifest versions ---------------------------------------------------------------------------


async def next_version(session: AsyncSession, project_id: UUID) -> int:
    statement = select(func.coalesce(func.max(_manifest.c.version), 0)).where(
        _manifest.c.project_id == project_id
    )
    return int((await session.execute(statement)).scalar_one()) + 1


async def insert_manifest(session: AsyncSession, manifest: ManifestVersion) -> None:
    await session.execute(
        insert(_manifest).values(
            id=manifest.id,
            project_id=manifest.project_id,
            version=manifest.version,
            status=manifest.status.value,
            content=manifest.content,
            content_hash=manifest.content_hash,
            created_by=manifest.created_by,
            created_at=manifest.created_at,
            updated_at=manifest.updated_at,
        )
    )


async def get_manifest(
    session: AsyncSession, project_id: UUID, version: int, *, for_update: bool = False
) -> ManifestVersion | None:
    statement = select(_manifest).where(
        _manifest.c.project_id == project_id, _manifest.c.version == version
    )
    if for_update:
        statement = statement.with_for_update()
    row = (await session.execute(statement)).first()
    return None if row is None else _to_manifest(row)


async def get_active(
    session: AsyncSession, project_id: UUID, *, for_update: bool = False
) -> ManifestVersion | None:
    statement = select(_manifest).where(
        _manifest.c.project_id == project_id,
        _manifest.c.status == ManifestStatus.ACTIVE.value,
    )
    if for_update:
        statement = statement.with_for_update()
    row = (await session.execute(statement)).first()
    return None if row is None else _to_manifest(row)


async def list_manifests(
    session: AsyncSession, project_id: UUID, *, cursor: Cursor | None, limit: int
) -> Sequence[ManifestVersion]:
    """Newest first, ``limit`` rows at most (callers pass ``limit + 1`` to detect more)."""
    statement = select(_manifest).where(_manifest.c.project_id == project_id)
    if cursor is not None:
        statement = statement.where(
            keyset_predicate(_manifest.c.created_at, _manifest.c.id, cursor)
        )
    statement = statement.order_by(_manifest.c.created_at.desc(), _manifest.c.id.desc()).limit(
        limit
    )
    return [_to_manifest(row) for row in (await session.execute(statement)).all()]


async def update_manifest(
    session: AsyncSession, project_id: UUID, version: int, **values: Any
) -> ManifestVersion:
    """Write lifecycle columns (the trigger enforces the transition and immutability)."""
    statement = (
        update(_manifest)
        .where(_manifest.c.project_id == project_id, _manifest.c.version == version)
        .values(**values)
        .returning(*_manifest.c)
    )
    return _to_manifest((await session.execute(statement)).one())


# -- activations and materialized endpoints ------------------------------------------------------


async def insert_activation(session: AsyncSession, activation: Activation) -> None:
    await session.execute(
        insert(_activation).values(
            id=activation.id,
            project_id=activation.project_id,
            version=activation.version,
            previous_version=activation.previous_version,
            activated_by=activation.activated_by,
            activated_at=activation.activated_at,
            materialized=activation.materialized,
        )
    )


async def list_activations(
    session: AsyncSession, project_id: UUID, *, limit: int
) -> Sequence[Activation]:
    statement = (
        select(_activation)
        .where(_activation.c.project_id == project_id)
        .order_by(_activation.c.activated_at.desc(), _activation.c.id.desc())
        .limit(limit)
    )
    return [_to_activation(row) for row in (await session.execute(statement)).all()]


async def materialized_endpoints(session: AsyncSession, project_id: UUID) -> dict[str, str]:
    """Fingerprint -> endpoint public id of the endpoints manifests already created."""
    statement = select(_endpoint.c.fingerprint, _endpoint.c.endpoint_id).where(
        _endpoint.c.project_id == project_id
    )
    return {row.fingerprint: row.endpoint_id for row in (await session.execute(statement)).all()}


async def insert_materialized_endpoint(
    session: AsyncSession,
    *,
    row_id: UUID,
    project_id: UUID,
    fingerprint: str,
    endpoint_id: str,
    url: str,
    event_types: list[str],
    version: int,
    now: datetime,
) -> None:
    await session.execute(
        insert(_endpoint).values(
            id=row_id,
            project_id=project_id,
            fingerprint=fingerprint,
            endpoint_id=endpoint_id,
            url=url,
            event_types=event_types,
            version=version,
            created_at=now,
        )
    )


# -- business events -----------------------------------------------------------------------------


async def get_business_event(
    session: AsyncSession, project_id: UUID, name: str, idempotency_key: str
) -> BusinessEventRecord | None:
    statement = select(_event).where(
        _event.c.project_id == project_id,
        _event.c.name == name,
        _event.c.idempotency_key == idempotency_key,
    )
    row = (await session.execute(statement)).first()
    return None if row is None else _to_event(row)


async def insert_business_event(session: AsyncSession, record: BusinessEventRecord) -> bool:
    """``False`` when ``(project, name, idempotency_key)`` is already stored."""
    statement = (
        insert(_event)
        .values(
            id=record.id,
            project_id=record.project_id,
            name=record.name,
            idempotency_key=record.idempotency_key,
            payload_hash=record.payload_hash,
            payload=record.payload,
            manifest_version=record.manifest_version,
            outcome=record.outcome,
            received_at=record.received_at,
        )
        .on_conflict_do_nothing(
            index_elements=[_event.c.project_id, _event.c.name, _event.c.idempotency_key]
        )
        .returning(_event.c.id)
    )
    return (await session.execute(statement)).first() is not None


async def list_business_events(
    session: AsyncSession, project_id: UUID, *, cursor: Cursor | None, limit: int
) -> Sequence[BusinessEventRecord]:
    statement = select(_event).where(_event.c.project_id == project_id)
    if cursor is not None:
        statement = statement.where(keyset_predicate(_event.c.received_at, _event.c.id, cursor))
    statement = statement.order_by(_event.c.received_at.desc(), _event.c.id.desc()).limit(limit)
    return [_to_event(row) for row in (await session.execute(statement)).all()]
