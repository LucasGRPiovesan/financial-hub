"""The only place provider credentials are decrypted (import-linter contract 5).

Provider adapters (``asaas`` and later) call ``reveal_credentials`` right before talking to the
provider and drop the result immediately. Nothing here is reachable from an HTTP route: the API
layer and the application layer can encrypt (``platform.crypto.seal``) but never decrypt.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from financial_hub.modules.projects.domain import credentials_aad, decode_credentials
from financial_hub.modules.projects.infrastructure import provider_repository
from financial_hub.platform.crypto.keyring import DecryptionError, Keyring
from financial_hub.platform.crypto.unseal import unseal


class CredentialsUnavailableError(Exception):
    """The account does not exist in the project, or its credentials no longer decrypt."""


async def reveal_credentials(
    session: AsyncSession, keyring: Keyring, *, project_id: UUID, account_id: UUID
) -> dict[str, str]:
    loaded = await provider_repository.load_sealed(session, project_id, account_id)
    if loaded is None:
        raise CredentialsUnavailableError
    account, sealed = loaded
    aad = credentials_aad(project_id, account.provider, account.id)
    try:
        return decode_credentials(unseal(keyring, sealed, aad=aad))
    except DecryptionError, ValueError:
        raise CredentialsUnavailableError from None
