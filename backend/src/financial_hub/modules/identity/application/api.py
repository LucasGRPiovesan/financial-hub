"""Public API of identity for other modules (ARCHITECTURE s3): import only from here.

Deliberately thin (domain types + membership writes on the caller's session) so that modules
which identity itself calls, like projects, can depend on it without an import cycle.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from financial_hub.modules.identity.domain import Permission, Role
from financial_hub.modules.identity.infrastructure import repository

__all__ = ["Permission", "Role", "grant_membership"]


async def grant_membership(
    session: AsyncSession, *, operator_id: UUID, project_id: UUID, role: Role
) -> None:
    """Add ``operator_id`` to ``project_id`` with ``role`` inside the caller's transaction."""
    await repository.insert_membership(
        session, operator_id=operator_id, project_id=project_id, role=role
    )
