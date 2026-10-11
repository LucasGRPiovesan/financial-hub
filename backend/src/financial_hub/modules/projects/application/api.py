"""Public API of projects for other modules (ARCHITECTURE s3): import only from here.

Deliberately thin (reads on the caller's session) so identity can use it without an import cycle.
"""

from collections.abc import Collection, Mapping
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from financial_hub.modules.projects.infrastructure import repository

__all__ = ["project_names"]


async def project_names(session: AsyncSession, project_ids: Collection[UUID]) -> Mapping[UUID, str]:
    """Display names of the given projects (unknown ids are absent)."""
    return await repository.project_names(session, project_ids)
