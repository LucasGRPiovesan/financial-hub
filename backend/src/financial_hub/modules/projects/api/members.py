"""``/v1/projects/{project_id}/members``: who can act in a project, and as what (operator-only).

Reads need ``members:read`` (every role); writes need ``members:manage`` (owner, admin). API keys
are rejected on every route: access control is a human decision, never a machine credential's.
Granting, changing or removing the ``owner`` role is further restricted to owners, and a project
always keeps one owner (409 otherwise).
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field

from financial_hub.modules.audit.application import (
    Actor,
    AuditContext,
    request_audit_context,
)
from financial_hub.modules.projects.application.members import (
    OPERATOR_PRINCIPAL,
    MembersService,
    MemberView,
)
from financial_hub.modules.projects.application.service import Permission
from financial_hub.platform.db import UnitOfWork
from financial_hub.platform.http.auth import ProjectScope, require
from financial_hub.platform.http.protection.idempotency import idempotent
from financial_hub.shared.errors import Forbidden, NotFound
from financial_hub.shared.ids import IdPrefix, InvalidIdError, parse_id

_EMAIL_MAX = 320


class MemberOut(BaseModel):
    operator_id: str
    display_name: str
    email: str
    role: str
    added_at: datetime

    @classmethod
    def of(cls, view: MemberView) -> MemberOut:
        return cls(
            operator_id=view.operator_id,
            display_name=view.display_name,
            email=view.email,
            role=view.role,
            added_at=view.added_at,
        )


class MembersOut(BaseModel):
    data: list[MemberOut]


class AddMemberRequest(BaseModel):
    email: str = Field(min_length=1, max_length=_EMAIL_MAX)
    role: str = Field(min_length=1, max_length=20)


class ChangeRoleRequest(BaseModel):
    role: str = Field(min_length=1, max_length=20)


AuditCtx = Annotated[AuditContext, Depends(request_audit_context)]
ReadScope = Annotated[ProjectScope, require(Permission.MEMBERS_READ)]
ManageScope = Annotated[ProjectScope, require(Permission.MEMBERS_MANAGE)]

router = APIRouter(prefix="/v1/projects/{project_id}/members", tags=["members"])


def _service(uow: UnitOfWork, scope: ProjectScope, audit: AuditContext) -> MembersService:
    if scope.principal_kind != OPERATOR_PRINCIPAL:
        raise Forbidden("Membership is managed by operators only.")
    actor = Actor.of_principal(scope.principal_kind, scope.principal_id)
    return MembersService(uow, audit.acting(actor), scope.principal_id)


def _member_id(raw: str) -> UUID:
    try:
        return parse_id(raw, IdPrefix.OPERATOR)
    except InvalidIdError:
        raise NotFound("Member not found.") from None


def _uow(request: Request) -> UnitOfWork:
    return UnitOfWork(request.app.state.session_factory)


@router.get("")
async def list_members(scope: ReadScope, request: Request, audit: AuditCtx) -> MembersOut:
    async with _uow(request) as uow:
        members = await _service(uow, scope, audit).list_members(scope.project_id)
    return MembersOut(data=[MemberOut.of(view) for view in members])


@router.post("", status_code=status.HTTP_201_CREATED)
async def add_member(
    body: AddMemberRequest,
    scope: ManageScope,
    request: Request,
    audit: AuditCtx,
    _idempotency: Annotated[None, idempotent()],
) -> MemberOut:
    async with _uow(request) as uow:
        view = await _service(uow, scope, audit).add(
            scope.project_id, email=body.email, role=body.role
        )
    return MemberOut.of(view)


@router.patch("/{operator_id}")
async def change_member_role(
    operator_id: str,
    body: ChangeRoleRequest,
    scope: ManageScope,
    request: Request,
    audit: AuditCtx,
) -> MemberOut:
    async with _uow(request) as uow:
        view = await _service(uow, scope, audit).change_role(
            scope.project_id, _member_id(operator_id), role=body.role
        )
    return MemberOut.of(view)


@router.delete("/{operator_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    operator_id: str, scope: ManageScope, request: Request, audit: AuditCtx
) -> Response:
    async with _uow(request) as uow:
        await _service(uow, scope, audit).remove(scope.project_id, _member_id(operator_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
