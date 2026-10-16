"""Loads what a provider needs to register one customer (W4.4; import-linter contract 5).

Sits between the document vault (the only decryptor) and the application API, like the projects
credentials loader: other modules reach it only through ``customers.application.api``, and the
document it returns is meant to be handed to a provider call and dropped.
"""

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from financial_hub.modules.customers.infrastructure import repository
from financial_hub.modules.customers.infrastructure.document_vault import (
    DocumentUnavailableError,
    reveal_document,
)
from financial_hub.platform.crypto.keyring import Keyring


@dataclass(frozen=True, slots=True)
class CustomerContact:
    """A live identity's contact data plus its decrypted CPF/CNPJ; ``repr`` never shows PII."""

    identity_id: UUID
    name: str = field(repr=False)
    email: str | None = field(repr=False)
    phone: str | None = field(repr=False)
    document: str | None = field(repr=False)

    def __repr__(self) -> str:
        return f"CustomerContact(identity_id={self.identity_id!s}, <redacted>)"

    __str__ = __repr__


async def load_contact(
    session: AsyncSession, keyring: Keyring, *, project_id: UUID, identity_id: UUID
) -> CustomerContact | None:
    """The identity's contact, or ``None`` when it is absent, another project's or anonymized.

    ``document`` is ``None`` when the identity has no document (or it no longer decrypts): the
    provider decides whether it can register the customer without one.
    """
    identity = await repository.get_identity(session, project_id, identity_id)
    if identity is None or identity.anonymized_at is not None:
        return None
    try:
        document: str | None = await reveal_document(
            session, keyring, project_id=project_id, identity_id=identity_id
        )
    except DocumentUnavailableError:
        document = None
    return CustomerContact(
        identity_id=identity.id,
        name=identity.name,
        email=identity.email,
        phone=identity.phone,
        document=document,
    )
