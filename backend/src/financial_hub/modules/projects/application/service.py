"""Project use cases. Every method runs inside the caller's active :class:`UnitOfWork`."""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from financial_hub.modules.audit.application import (
    AuditContext,
    AuditRecorder,
    AuditTarget,
    changed_fields,
)
from financial_hub.modules.identity.application.api import Permission, Role, grant_membership
from financial_hub.modules.projects.domain import (
    Project,
    ProjectStatus,
    validate_name,
    validate_slug,
)
from financial_hub.modules.projects.infrastructure import repository
from financial_hub.platform.db import UnitOfWork
from financial_hub.platform.http.auth import Principal
from financial_hub.shared.errors import Conflict, FieldError, Forbidden, NotFound, ValidationFailed
from financial_hub.shared.ids import IdPrefix, format_id, new_uuid, parse_id
from financial_hub.shared.pagination import Cursor, Page, PageParams, build_page

__all__ = ["Permission", "ProjectService", "ProjectView"]

OPERATOR_PRINCIPAL = "operator"


@dataclass(frozen=True, slots=True)
class ProjectView:
    id: str
    slug: str
    name: str
    status: str
    created_at: datetime


def _view(project: Project) -> ProjectView:
    return ProjectView(
        id=format_id(IdPrefix.PROJECT, project.id),
        slug=project.slug,
        name=project.name,
        status=project.status.value,
        created_at=project.created_at,
    )


def _invalid(pointer: str, error: ValueError) -> ValidationFailed:
    return ValidationFailed(
        "The request contains invalid fields.",
        errors=[FieldError(pointer=pointer, detail=str(error), code="invalid")],
    )


class ProjectService:
    def __init__(self, uow: UnitOfWork, audit: AuditContext | None = None) -> None:
        self._uow = uow
        self._audit = audit or AuditContext.system()

    async def create(self, *, owner_operator_id: UUID, slug: str, name: str) -> ProjectView:
        """Create a project and make ``owner_operator_id`` its owner (same transaction)."""
        try:
            slug = validate_slug(slug)
        except ValueError as error:
            raise _invalid("/slug", error) from None
        try:
            name = validate_name(name)
        except ValueError as error:
            raise _invalid("/name", error) from None
        session = self._uow.session
        project = await repository.insert_project(
            session, project_id=new_uuid(), slug=slug, name=name
        )
        if project is None:
            raise Conflict(
                "A project with this slug already exists.",
                errors=[FieldError(pointer="/slug", detail="already taken", code="taken")],
            )
        await grant_membership(
            session, operator_id=owner_operator_id, project_id=project.id, role=Role.OWNER
        )
        owner = format_id(IdPrefix.OPERATOR, owner_operator_id)
        project_id = format_id(IdPrefix.PROJECT, project.id)
        await AuditRecorder.record(
            session,
            actor=self._audit,
            action="project.created",
            target=AuditTarget("project", project_id),
            diff=changed_fields(None, {"slug": project.slug, "name": project.name}),
            project_id=project.id,
        )
        await AuditRecorder.record(
            session,
            actor=self._audit,
            action="member.added",
            target=AuditTarget("operator", owner),
            diff=changed_fields(None, {"role": Role.OWNER.value}),
            project_id=project.id,
        )
        return _view(project)

    async def create_owned_by(self, principal: Principal, *, slug: str, name: str) -> ProjectView:
        """Projects are created by operators only; the caller becomes ``owner``."""
        if principal.kind != OPERATOR_PRINCIPAL:
            raise Forbidden("Only operators can create projects.")
        owner = parse_id(principal.subject_id, IdPrefix.OPERATOR)
        return await self.create(owner_operator_id=owner, slug=slug, name=name)

    async def list_visible(
        self, project_ids: Collection[UUID], params: PageParams
    ) -> Page[ProjectView]:
        rows = await repository.list_projects(
            self._uow.session, project_ids, cursor=params.decoded_cursor(), limit=params.limit + 1
        )
        return build_page(
            rows, params.limit, _view, lambda row: Cursor(created_at=row.created_at, id=row.id)
        )

    async def find_by_slug(self, slug: str) -> ProjectView | None:
        project = await repository.get_project_by_slug(self._uow.session, slug)
        return None if project is None else _view(project)

    async def get(self, project_id: UUID) -> ProjectView:
        project = await repository.get_project(self._uow.session, project_id)
        if project is None:
            raise NotFound("Project not found.")
        return _view(project)

    async def update(
        self, project_id: UUID, *, name: str | None = None, status: str | None = None
    ) -> ProjectView:
        """Rename and/or archive/reactivate; at least one change is required."""
        if name is None and status is None:
            raise ValidationFailed(
                "The request contains invalid fields.",
                errors=[
                    FieldError(
                        pointer="/", detail="provide name and/or status", code="empty_update"
                    )
                ],
            )
        new_name: str | None = None
        if name is not None:
            try:
                new_name = validate_name(name)
            except ValueError as error:
                raise _invalid("/name", error) from None
        new_status: ProjectStatus | None = None
        if status is not None:
            try:
                new_status = ProjectStatus(status)
            except ValueError:
                raise _invalid("/status", ValueError("must be active or archived")) from None
        before = await repository.get_project(self._uow.session, project_id)
        project = await repository.update_project(
            self._uow.session, project_id, name=new_name, status=new_status
        )
        if project is None or before is None:
            raise NotFound("Project not found.")
        diff = changed_fields(
            {"name": before.name, "status": before.status.value},
            {"name": project.name, "status": project.status.value},
        )
        if diff["after"]:  # an update that changes nothing is not an audited change
            await AuditRecorder.record(
                self._uow.session,
                actor=self._audit,
                action="project.updated",
                target=AuditTarget("project", format_id(IdPrefix.PROJECT, project.id)),
                diff=diff,
                project_id=project.id,
            )
        return _view(project)
