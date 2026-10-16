"""Loads one provider account's secrets for a payment adapter (PRO-41; import-linter contract 5).

The loader sits between the vault (the only decryptor) and the application API: it answers only for
an account that belongs to the asking project **and** is active, so a disabled or foreign account
looks exactly like a missing one. Reached from outside projects only through
``projects.application.api``; payments never imports it (it cannot see decryption).
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from financial_hub.modules.projects.domain import ProviderAccountStatus
from financial_hub.modules.projects.infrastructure import provider_repository
from financial_hub.modules.projects.infrastructure.credentials_vault import (
    CredentialsUnavailableError,
    reveal_credentials,
)
from financial_hub.platform.crypto.keyring import Keyring


@dataclass(frozen=True, slots=True)
class ProviderSecrets:
    """Decrypted credentials plus the account's non-secret identity; ``repr`` never shows values."""

    provider: str
    environment: str
    values: Mapping[str, str] = field(repr=False)

    def __repr__(self) -> str:
        return f"ProviderSecrets(provider={self.provider!r}, environment={self.environment!r})"


async def load_active_secrets(
    session: AsyncSession, keyring: Keyring, *, project_id: UUID, account_id: UUID
) -> ProviderSecrets | None:
    """The account's secrets, or ``None`` if it is absent, another project's, disabled or sealed
    under a key that no longer opens it."""
    account = await provider_repository.get_account(session, project_id, account_id)
    if account is None or account.status is not ProviderAccountStatus.ACTIVE:
        return None
    try:
        values = await reveal_credentials(
            session, keyring, project_id=project_id, account_id=account_id
        )
    except CredentialsUnavailableError:
        return None
    return ProviderSecrets(
        provider=account.provider.value, environment=account.environment.value, values=values
    )
