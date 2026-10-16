"""Project persistence on the caller's session (runs as ``fh_app``: no DELETE)."""

from collections.abc import Collection, Mapping, Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from financial_hub.modules.projects.domain import Project, ProjectStatus
from financial_hub.modules.projects.infrastructure.tables import projects
from financial_hub.shared.pagination import Cursor, keyset_predicate

_COLUMNS = (
    projects.c.id,
    projects.c.slug,
    projects.c.name,
    projects.c.status,
    projects.c.created_at,
)


def _project(row: Any) -> Project:
    return Project(
        id=row.id,
        slug=row.slug,
        name=row.name,
        status=ProjectStatus(row.status),
        created_at=row.created_at,
    )


async def insert_project(
    session: AsyncSession, *, project_id: UUID, slug: str, name: str
) -> Project | None:
    """``None`` when the slug is taken (nothing is written)."""
    statement = (
        insert(projects)
        .values(id=project_id, slug=slug, name=name, status=ProjectStatus.ACTIVE.value)
        .on_conflict_do_nothing(index_elements=[projects.c.slug])
        .returning(*_COLUMNS)
    )
    row = (await session.execute(statement)).first()
    return None if row is None else _project(row)


async def get_project(session: AsyncSession, project_id: UUID) -> Project | None:
    row = (await session.execute(select(*_COLUMNS).where(projects.c.id == project_id))).first()
    return None if row is None else _project(row)


async def list_projects(
    session: AsyncSession, project_ids: Collection[UUID], *, cursor: Cursor | None, limit: int
) -> Sequence[Project]:
    """Newest first, ``limit`` rows at most (callers pass ``limit + 1`` to detect more)."""
    if not project_ids:
        return []
    statement = select(*_COLUMNS).where(projects.c.id.in_(list(project_ids)))
    if cursor is not None:
        statement = statement.where(keyset_predicate(projects.c.created_at, projects.c.id, cursor))
    statement = statement.order_by(projects.c.created_at.desc(), projects.c.id.desc()).limit(limit)
    return [_project(row) for row in (await session.execute(statement)).all()]


async def project_names(session: AsyncSession, project_ids: Collection[UUID]) -> Mapping[UUID, str]:
    if not project_ids:
        return {}
    statement = select(projects.c.id, projects.c.name).where(projects.c.id.in_(list(project_ids)))
    return {row.id: row.name for row in (await session.execute(statement)).all()}


async def update_project(
    session: AsyncSession,
    project_id: UUID,
    *,
    name: str | None = None,
    status: ProjectStatus | None = None,
) -> Project | None:
    """Apply the given changes (at least one); ``None`` when the project does not exist."""
    values: dict[str, Any] = {"updated_at": func.now()}
    if name is not None:
        values["name"] = name
    if status is not None:
        values["status"] = status.value
    statement = (
        update(projects).where(projects.c.id == project_id).values(**values).returning(*_COLUMNS)
    )
    row = (await session.execute(statement)).first()
    return None if row is None else _project(row)


async def get_project_by_slug(session: AsyncSession, slug: str) -> Project | None:
    row = (await session.execute(select(*_COLUMNS).where(projects.c.slug == slug))).first()
    return None if row is None else _project(row)
