"""The only place webhook signing secrets are decrypted (import-linter contract 5).

The sender calls ``signing_secrets`` right before signing a delivery and drops the result after.
Nothing here is reachable from an HTTP route: secrets are shown once, at create/rotate time, from
the freshly generated value, never read back.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from financial_hub.modules.webhooks.domain import secret_aad
from financial_hub.modules.webhooks.infrastructure import repository
from financial_hub.platform.crypto.keyring import DecryptionError, Keyring
from financial_hub.platform.crypto.unseal import unseal


class SecretUnavailableError(Exception):
    """The endpoint does not exist, or its secret no longer decrypts (key removed)."""


async def signing_secrets(
    session: AsyncSession, keyring: Keyring, endpoint_id: UUID, now: datetime
) -> list[str]:
    """The secrets to sign with: current first, then the previous one inside its overlap."""
    sealed = await repository.load_sealed_secrets(session, endpoint_id)
    if sealed is None:
        raise SecretUnavailableError
    aad = secret_aad(endpoint_id)
    candidates = [sealed.current]
    if (
        sealed.previous is not None
        and sealed.previous_expires_at is not None
        and now < sealed.previous_expires_at
    ):
        candidates.append(sealed.previous)
    try:
        return [unseal(keyring, item, aad=aad).decode("ascii") for item in candidates]
    except DecryptionError, UnicodeDecodeError:
        raise SecretUnavailableError from None
