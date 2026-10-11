"""``/v1/projects``: the caller's projects (operator level) and per-project reads/updates.

Each handler runs one Unit of Work and commits it before the response is built.
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Request, status
from pydantic import BaseModel, Field

from financial_hub.modules.projects.application.service import (
    Permission,
    ProjectService,
    ProjectView,
)
from financial_hub.platform.db import UnitOfWork
from financial_hub.platform.http.auth import Principal, ProjectScope, authenticated, require
from financial_hub.shared.pagination import Page, PageParams


class ProjectOut(BaseModel):
    id: str
    slug: str
    name: str
    status: str
    created_at: datetime

    @classmethod
    def of(cls, view: ProjectView) -> ProjectOut:
        return cls(
            id=view.id,
            slug=view.slug,
            name=view.name,
            status=view.status,
            created_at=view.created_at,
        )


class CreateProjectRequest(BaseModel):
    slug: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)


class UpdateProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)


def _uow(request: Request) -> UnitOfWork:
    return UnitOfWork(request.app.state.session_factory)


CurrentPrincipal = Annotated[Principal, authenticated()]

router = APIRouter(prefix="/v1/projects", tags=["projects"])


@router.get("")
async def list_projects(
    principal: CurrentPrincipal, params: Annotated[PageParams, Query()], request: Request
) -> Page[ProjectOut]:
    async with _uow(request) as uow:
        page = await ProjectService(uow).list_visible(principal.project_ids, params)
    return Page[ProjectOut](
        data=[ProjectOut.of(view) for view in page.data],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_project(
    body: CreateProjectRequest, principal: CurrentPrincipal, request: Request
) -> ProjectOut:
    async with _uow(request) as uow:
        view = await ProjectService(uow).create_owned_by(principal, slug=body.slug, name=body.name)
    return ProjectOut.of(view)


@router.get("/{project_id}")
async def get_project(
    scope: Annotated[ProjectScope, require(Permission.PROJECTS_READ)], request: Request
) -> ProjectOut:
    async with _uow(request) as uow:
        view = await ProjectService(uow).get(scope.project_id)
    return ProjectOut.of(view)


@router.patch("/{project_id}")
async def update_project(
    body: UpdateProjectRequest,
    scope: Annotated[ProjectScope, require(Permission.PROJECTS_MANAGE)],
    request: Request,
) -> ProjectOut:
    async with _uow(request) as uow:
        view = await ProjectService(uow).rename(scope.project_id, body.name)
    return ProjectOut.of(view)
