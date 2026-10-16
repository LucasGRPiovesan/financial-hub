"""Public API of projects for other modules (ARCHITECTURE s3): import only from here.

Deliberately thin (reads on the caller's session) so identity can use it without an import cycle.
Nothing here returns credential material: provider accounts are exposed as
:class:`ProviderAccountRef`.
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from financial_hub.modules.projects.domain import ProviderAccount, ProviderAccountStatus
from financial_hub.modules.projects.infrastructure import provider_repository, repository

__all__ = [
    "ProviderAccountRef",
    "active_provider_accounts",
    "project_names",
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


async def active_provider_accounts(
    session: AsyncSession, project_id: UUID
) -> Sequence[ProviderAccountRef]:
    """The project's active provider accounts, oldest first."""
    accounts = await provider_repository.list_active_accounts(session, project_id)
    return [_ref(account) for account in accounts]
