"""Public API of identity for other modules (ARCHITECTURE s3): import only from here.

Deliberately thin (domain types + membership reads/writes on the caller's session) so that modules
which identity itself calls, like projects, can depend on it without an import cycle.
"""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from financial_hub.modules.identity.domain import (
    Operator,
    OperatorStatus,
    Permission,
    Role,
    is_valid_email,
    normalize_email,
)
from financial_hub.modules.identity.infrastructure import repository
from financial_hub.modules.identity.infrastructure.repository import MemberRow

__all__ = [
    "MemberRow",
    "Operator",
    "OperatorStatus",
    "Permission",
    "Role",
    "change_member_role",
    "find_operator_by_email",
    "grant_membership",
    "is_valid_email",
    "normalize_email",
    "project_members",
    "remove_membership",
]


async def grant_membership(
    session: AsyncSession, *, operator_id: UUID, project_id: UUID, role: Role
) -> None:
    """Add ``operator_id`` to ``project_id`` with ``role`` inside the caller's transaction."""
    await repository.insert_membership(
        session, operator_id=operator_id, project_id=project_id, role=role
    )


async def find_operator_by_email(session: AsyncSession, email: str) -> Operator | None:
    return await repository.get_operator_by_email(session, normalize_email(email))


async def project_members(
    session: AsyncSession, project_id: UUID, *, for_update: bool = False
) -> Sequence[MemberRow]:
    """The members of ``project_id``; ``for_update`` locks them for a membership change."""
    return await repository.project_members(session, project_id, for_update=for_update)


async def change_member_role(
    session: AsyncSession, *, operator_id: UUID, project_id: UUID, role: Role
) -> None:
    await repository.set_membership_role(
        session, operator_id=operator_id, project_id=project_id, role=role
    )


async def remove_membership(session: AsyncSession, *, operator_id: UUID, project_id: UUID) -> None:
    await repository.delete_membership(session, operator_id=operator_id, project_id=project_id)
