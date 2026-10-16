"""The only place customer documents are decrypted (import-linter contract 5).

Provider adapters call ``reveal_document`` right before talking to a provider and drop the result.
Nothing reachable from an HTTP route imports this module: API, application and domain can seal
(``platform.crypto.seal``) but never open.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from financial_hub.modules.customers.domain import document_aad
from financial_hub.modules.customers.infrastructure import repository
from financial_hub.platform.crypto.keyring import DecryptionError, Keyring
from financial_hub.platform.crypto.unseal import unseal


class DocumentUnavailableError(Exception):
    """The identity has no document in the project, or the stored one no longer decrypts."""


async def reveal_document(
    session: AsyncSession, keyring: Keyring, *, project_id: UUID, identity_id: UUID
) -> str:
    loaded = await repository.load_sealed_document(session, project_id, identity_id)
    if loaded is None:
        raise DocumentUnavailableError
    customer_id, sealed = loaded
    try:
        return unseal(keyring, sealed, aad=document_aad(customer_id)).decode("ascii")
    except DecryptionError, ValueError:
        raise DocumentUnavailableError from None
