"""Public API of projects for other modules (ARCHITECTURE s3): import only from here.

Deliberately thin (reads on the caller's session) so identity can use it without an import cycle.
Provider accounts are exposed as :class:`ProviderAccountRef` (no credential material). The single
exception is :func:`load_provider_secrets`, which only the composition root wires into a payment
provider adapter's credentials loader (ADR-0006; PRO-41).
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from financial_hub.modules.projects.domain import ProviderAccount, ProviderAccountStatus
from financial_hub.modules.projects.infrastructure import (
    credentials_loader,
    provider_repository,
    repository,
)
from financial_hub.modules.projects.infrastructure.credentials_loader import ProviderSecrets
from financial_hub.platform.crypto.keyring import Keyring

__all__ = [
    "ProviderAccountRef",
    "ProviderSecrets",
    "active_provider_accounts",
    "load_provider_secrets",
    "project_names",
    "provider_account_by_id",
    "provider_account_ref",
]


@dataclass(frozen=True, slots=True)
class ProviderAccountRef:
    """A provider account's identity (no name, hint or credentials): enough to route a call."""

    id: UUID
    project_id: UUID
    provider: str
    environment: str
    active: bool


def _ref(account: ProviderAccount) -> ProviderAccountRef:
    return ProviderAccountRef(
        id=account.id,
        project_id=account.project_id,
        provider=account.provider.value,
        environment=account.environment.value,
        active=account.status is ProviderAccountStatus.ACTIVE,
    )


async def project_names(session: AsyncSession, project_ids: Collection[UUID]) -> Mapping[UUID, str]:
    """Display names of the given projects (unknown ids are absent)."""
    return await repository.project_names(session, project_ids)


async def provider_account_ref(
    session: AsyncSession, project_id: UUID, account_id: UUID
) -> ProviderAccountRef | None:
    """The project's provider account ``account_id`` (``None`` if absent or another project's)."""
    account = await provider_repository.get_account(session, project_id, account_id)
    return None if account is None else _ref(account)


async def provider_account_by_id(
    session: AsyncSession, account_id: UUID
) -> ProviderAccountRef | None:
    """Any project's provider account by id, to route an inbound provider webhook (W4.4).

    Not project-scoped: the caller must authenticate the request (the account's webhook token)
    before acting on it, and must treat an absent account exactly like a failed authentication.
    """
    account = await provider_repository.find_account(session, account_id)
    return None if account is None else _ref(account)


async def load_provider_secrets(
    session: AsyncSession, keyring: Keyring, *, project_id: UUID, account_id: UUID
) -> ProviderSecrets | None:
    """Decrypted credentials of an *active* account of ``project_id`` (``None`` otherwise).

    The one door to credential material for provider adapters' callers (the composition root wires
    it into payments); it never reveals a disabled, foreign or undecryptable account.
    """
    return await credentials_loader.load_active_secrets(
        session, keyring, project_id=project_id, account_id=account_id
    )


async def active_provider_accounts(
    session: AsyncSession, project_id: UUID
) -> Sequence[ProviderAccountRef]:
    """The project's active provider accounts, oldest first."""
    accounts = await provider_repository.list_active_accounts(session, project_id)
    return [_ref(account) for account in accounts]
