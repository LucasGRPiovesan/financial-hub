"""Customer persistence on the caller's session (runs as ``fh_app``: no DELETE, column UPDATE).

Every identity read is scoped by ``project_id``. Reads never touch the document ciphertext; the
one function that does, ``load_sealed_document``, feeds ``document_vault``.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from financial_hub.modules.customers.domain import CustomerIdentity, CustomerKind
from financial_hub.modules.customers.infrastructure.tables import (
    customer_identities,
    financial_customers,
    provider_customers,
)
from financial_hub.platform.crypto.seal import SealedSecret
from financial_hub.shared.pagination import Cursor, keyset_predicate

ANONYMIZED_NAME = "anonymized"

_identity = customer_identities
_universal = financial_customers

_COLUMNS = (
    _identity.c.id,
    _identity.c.project_id,
    _identity.c.financial_customer_id,
    _identity.c.external_ref,
    _identity.c.kind,
    _identity.c.name,
    _identity.c.email,
    _identity.c.phone,
    _identity.c.metadata,
    _identity.c.anonymized_at,
    _identity.c.created_at,
    _identity.c.updated_at,
    _universal.c.document_last4,
)
_JOIN = _identity.join(_universal, _universal.c.id == _identity.c.financial_customer_id)


def _to_identity(row: Any) -> CustomerIdentity:
    return CustomerIdentity(
        id=row.id,
        project_id=row.project_id,
        financial_customer_id=row.financial_customer_id,
        external_ref=row.external_ref,
        kind=CustomerKind(row.kind),
        name=row.name,
        email=row.email,
        phone=row.phone,
        metadata=dict(row.metadata),
        document_last4=row.document_last4,
        anonymized_at=row.anonymized_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def advisory_lock(session: AsyncSession, key: str) -> None:
    """Transaction-scoped lock on ``key``: serialises writers of the same logical record."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key}
    )


async def get_identity_by_ref(
    session: AsyncSession, project_id: UUID, external_ref: str
) -> CustomerIdentity | None:
    statement = (
        select(*_COLUMNS)
        .select_from(_JOIN)
        .where(_identity.c.project_id == project_id, _identity.c.external_ref == external_ref)
    )
    row = (await session.execute(statement)).first()
    return None if row is None else _to_identity(row)


async def get_identity(
    session: AsyncSession, project_id: UUID, identity_id: UUID
) -> CustomerIdentity | None:
    statement = (
        select(*_COLUMNS)
        .select_from(_JOIN)
        .where(_identity.c.project_id == project_id, _identity.c.id == identity_id)
    )
    row = (await session.execute(statement)).first()
    return None if row is None else _to_identity(row)


async def list_identities(
    session: AsyncSession,
    project_id: UUID,
    *,
    external_ref: str | None,
    email: str | None,
    cursor: Cursor | None,
    limit: int,
) -> Sequence[CustomerIdentity]:
    """Newest first, ``limit`` rows at most (callers pass ``limit + 1`` to detect more)."""
    statement = select(*_COLUMNS).select_from(_JOIN).where(_identity.c.project_id == project_id)
    if external_ref is not None:
        statement = statement.where(_identity.c.external_ref == external_ref)
    if email is not None:
        statement = statement.where(_identity.c.email == email)
    if cursor is not None:
        statement = statement.where(
            keyset_predicate(_identity.c.created_at, _identity.c.id, cursor)
        )
    statement = statement.order_by(_identity.c.created_at.desc(), _identity.c.id.desc()).limit(
        limit
    )
    return [_to_identity(row) for row in (await session.execute(statement)).all()]


async def find_universal_by_hash(session: AsyncSession, document_hash: bytes) -> UUID | None:
    statement = select(_universal.c.id).where(_universal.c.document_hash == document_hash)
    return (await session.execute(statement)).scalar_one_or_none()


async def insert_universal(
    session: AsyncSession,
    *,
    customer_id: UUID,
    kind: CustomerKind,
    name: str,
    email: str | None,
    phone: str | None,
    document_hash: bytes | None,
    sealed: SealedSecret | None,
    document_last4: str | None,
    now: datetime,
) -> UUID:
    """Create the universal record; with a document, an existing holder of the hash wins.

    ``ON CONFLICT (document_hash) DO UPDATE`` is a no-op write that returns the winner's id, so two
    simultaneous upserts of the same document converge on one row even without the advisory lock.
    """
    statement = (
        insert(_universal)
        .values(
            id=customer_id,
            kind=kind.value,
            name=name,
            email=email,
            phone=phone,
            document_hash=document_hash,
            document_key_version=None if sealed is None else sealed.key_version,
            document_nonce=None if sealed is None else sealed.nonce,
            document_ciphertext=None if sealed is None else sealed.ciphertext,
            document_last4=document_last4,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=[_universal.c.document_hash],
            set_={"updated_at": _universal.c.updated_at},
        )
        .returning(_universal.c.id)
    )
    return (await session.execute(statement)).scalar_one()


async def attach_document(
    session: AsyncSession,
    customer_id: UUID,
    *,
    document_hash: bytes,
    sealed: SealedSecret,
    document_last4: str,
    now: datetime,
) -> bool:
    """Seal a document onto a universal record that has none (``False`` when it already has)."""
    statement = (
        update(_universal)
        .where(_universal.c.id == customer_id, _universal.c.document_hash.is_(None))
        .values(
            document_hash=document_hash,
            document_key_version=sealed.key_version,
            document_nonce=sealed.nonce,
            document_ciphertext=sealed.ciphertext,
            document_last4=document_last4,
            updated_at=now,
        )
        .returning(_universal.c.id)
    )
    return (await session.execute(statement)).first() is not None


async def insert_identity(
    session: AsyncSession,
    *,
    identity_id: UUID,
    project_id: UUID,
    customer_id: UUID,
    external_ref: str,
    kind: CustomerKind,
    name: str,
    email: str | None,
    phone: str | None,
    metadata: dict[str, Any],
    now: datetime,
) -> bool:
    """``False`` when ``(project_id, external_ref)`` already exists (a concurrent winner)."""
    statement = (
        insert(_identity)
        .values(
            id=identity_id,
            project_id=project_id,
            financial_customer_id=customer_id,
            external_ref=external_ref,
            kind=kind.value,
            name=name,
            email=email,
            phone=phone,
            metadata=metadata,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=[_identity.c.project_id, _identity.c.external_ref])
        .returning(_identity.c.id)
    )
    return (await session.execute(statement)).first() is not None


async def update_identity(
    session: AsyncSession,
    project_id: UUID,
    identity_id: UUID,
    *,
    customer_id: UUID,
    kind: CustomerKind,
    name: str,
    email: str | None,
    phone: str | None,
    metadata: dict[str, Any],
    now: datetime,
) -> None:
    await session.execute(
        update(_identity)
        .where(_identity.c.project_id == project_id, _identity.c.id == identity_id)
        .values(
            financial_customer_id=customer_id,
            kind=kind.value,
            name=name,
            email=email,
            phone=phone,
            metadata=metadata,
            updated_at=now,
        )
    )


async def lock_universal(session: AsyncSession, customer_id: UUID) -> None:
    await session.execute(
        select(_universal.c.id).where(_universal.c.id == customer_id).with_for_update()
    )


async def count_live_identities(
    session: AsyncSession, customer_id: UUID, *, excluding: UUID | None = None
) -> int:
    statement = (
        select(func.count())
        .select_from(_identity)
        .where(
            _identity.c.financial_customer_id == customer_id, _identity.c.anonymized_at.is_(None)
        )
    )
    if excluding is not None:
        statement = statement.where(_identity.c.id != excluding)
    return (await session.execute(statement)).scalar_one()


async def anonymize_identity(
    session: AsyncSession, project_id: UUID, identity_id: UUID, *, now: datetime
) -> None:
    await session.execute(
        update(_identity)
        .where(_identity.c.project_id == project_id, _identity.c.id == identity_id)
        .values(
            name=ANONYMIZED_NAME,
            email=None,
            phone=None,
            metadata={},
            anonymized_at=now,
            updated_at=now,
        )
    )


async def anonymize_universal(session: AsyncSession, customer_id: UUID, *, now: datetime) -> None:
    await session.execute(
        update(_universal)
        .where(_universal.c.id == customer_id)
        .values(
            name=ANONYMIZED_NAME,
            email=None,
            phone=None,
            document_hash=None,
            document_key_version=None,
            document_nonce=None,
            document_ciphertext=None,
            document_last4=None,
            anonymized_at=now,
            updated_at=now,
        )
    )


async def load_sealed_document(
    session: AsyncSession, project_id: UUID, identity_id: UUID
) -> tuple[UUID, SealedSecret] | None:
    """The universal id and sealed document behind an identity (``document_vault`` only)."""
    statement = (
        select(
            _universal.c.id,
            _universal.c.document_key_version,
            _universal.c.document_nonce,
            _universal.c.document_ciphertext,
        )
        .select_from(_JOIN)
        .where(_identity.c.project_id == project_id, _identity.c.id == identity_id)
    )
    row = (await session.execute(statement)).first()
    if row is None or row.document_ciphertext is None:
        return None
    sealed = SealedSecret(
        key_version=row.document_key_version,
        nonce=bytes(row.document_nonce),
        ciphertext=bytes(row.document_ciphertext),
    )
    return row.id, sealed


async def identity_in_project(session: AsyncSession, project_id: UUID, identity_id: UUID) -> bool:
    statement = select(_identity.c.id).where(
        _identity.c.project_id == project_id, _identity.c.id == identity_id
    )
    return (await session.execute(statement)).first() is not None


async def insert_provider_customer(
    session: AsyncSession,
    *,
    identity_id: UUID,
    provider_account_id: UUID,
    provider_customer_id: str,
    now: datetime,
) -> bool:
    """``False`` when the identity already has a mapping on that account or the id is taken."""
    statement = (
        insert(provider_customers)
        .values(
            identity_id=identity_id,
            provider_account_id=provider_account_id,
            provider_customer_id=provider_customer_id,
            created_at=now,
        )
        .on_conflict_do_nothing()
        .returning(provider_customers.c.identity_id)
    )
    return (await session.execute(statement)).first() is not None


async def get_provider_customer(
    session: AsyncSession, identity_id: UUID, provider_account_id: UUID
) -> str | None:
    statement = select(provider_customers.c.provider_customer_id).where(
        provider_customers.c.identity_id == identity_id,
        provider_customers.c.provider_account_id == provider_account_id,
    )
    return (await session.execute(statement)).scalar_one_or_none()


async def find_identity_by_provider_customer(
    session: AsyncSession, project_id: UUID, provider_account_id: UUID, provider_customer_id: str
) -> UUID | None:
    statement = (
        select(provider_customers.c.identity_id)
        .select_from(
            provider_customers.join(_identity, _identity.c.id == provider_customers.c.identity_id)
        )
        .where(
            _identity.c.project_id == project_id,
            provider_customers.c.provider_account_id == provider_account_id,
            provider_customers.c.provider_customer_id == provider_customer_id,
        )
    )
    return (await session.execute(statement)).scalar_one_or_none()
