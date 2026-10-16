"""Project membership use cases (``members:read`` / ``members:manage``).

Invariants, enforced inside one transaction that locks the project's membership rows (so two
concurrent changes serialise):

- a project always keeps at least one owner: the last owner can be neither demoted nor removed;
- only an owner can grant ownership, take it away, or remove an owner.

Every change is audited (``member.added``, ``member.role_changed``, ``member.removed``) in the same
transaction.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from financial_hub.modules.audit.application import (
    AuditContext,
    AuditRecorder,
    AuditTarget,
    changed_fields,
)
from financial_hub.modules.identity.application.api import (
    MemberRow,
    OperatorStatus,
    Role,
    change_member_role,
    find_operator_by_email,
    grant_membership,
    is_valid_email,
    normalize_email,
    project_members,
    remove_membership,
)
from financial_hub.platform.db import UnitOfWork
from financial_hub.shared.errors import Conflict, FieldError, Forbidden, NotFound, ValidationFailed
from financial_hub.shared.ids import IdPrefix, format_id, parse_id

__all__ = ["MemberView", "MembersService"]

OPERATOR_PRINCIPAL = "operator"


@dataclass(frozen=True, slots=True)
class MemberView:
    operator_id: str
    display_name: str
    email: str
    role: str
    added_at: datetime


def _view(row: MemberRow) -> MemberView:
    return MemberView(
        operator_id=format_id(IdPrefix.OPERATOR, row.operator_id),
        display_name=row.display_name,
        email=row.email,
        role=row.role.value,
        added_at=row.added_at,
    )


def _invalid(pointer: str, detail: str, code: str = "invalid") -> ValidationFailed:
    return ValidationFailed(
        "The request contains invalid fields.",
        errors=[FieldError(pointer=pointer, detail=detail, code=code)],
    )


def _parse_role(value: str) -> Role:
    try:
        return Role(value)
    except ValueError:
        raise _invalid("/role", "must be owner, admin, finance or viewer") from None


class MembersService:
    """``actor`` is the operator performing the change (its role gates owner changes)."""

    def __init__(self, uow: UnitOfWork, audit: AuditContext, actor: str) -> None:
        self._uow = uow
        self._audit = audit
        self._actor = actor

    async def list_members(self, project_id: UUID) -> list[MemberView]:
        return [_view(row) for row in await project_members(self._uow.session, project_id)]

    def _actor_role(self, members: Sequence[MemberRow]) -> Role | None:
        actor_id = parse_id(self._actor, IdPrefix.OPERATOR)
        return next((row.role for row in members if row.operator_id == actor_id), None)

    def _require_owner(self, members: Sequence[MemberRow]) -> None:
        if self._actor_role(members) is not Role.OWNER:
            raise Forbidden("Only an owner can grant, change or remove the owner role.")

    async def _record(
        self,
        action: str,
        project_id: UUID,
        operator_id: UUID,
        before: dict[str, str] | None,
        after: dict[str, str] | None,
    ) -> None:
        await AuditRecorder.record(
            self._uow.session,
            actor=self._audit,
            action=action,
            target=AuditTarget("operator", format_id(IdPrefix.OPERATOR, operator_id)),
            diff=changed_fields(before, after),
            project_id=project_id,
        )

    async def add(self, project_id: UUID, *, email: str, role: str) -> MemberView:
        new_role = _parse_role(role)
        normalized = normalize_email(email)
        if not is_valid_email(normalized):
            raise _invalid("/email", "not a valid email address")
        session = self._uow.session
        members = await project_members(session, project_id, for_update=True)
        if new_role is Role.OWNER:
            self._require_owner(members)
        operator = await find_operator_by_email(session, normalized)
        if operator is None or operator.status is not OperatorStatus.ACTIVE:
            raise _invalid("/email", "no active operator has this email", "unknown_operator")
        if any(row.operator_id == operator.id for row in members):
            raise Conflict(
                "This operator is already a member of the project.",
                errors=[FieldError(pointer="/email", detail="already a member", code="member")],
            )
        await grant_membership(
            session, operator_id=operator.id, project_id=project_id, role=new_role
        )
        await self._record("member.added", project_id, operator.id, None, {"role": new_role.value})
        added = await project_members(session, project_id)
        return _view(next(row for row in added if row.operator_id == operator.id))

    async def change_role(self, project_id: UUID, operator_id: UUID, *, role: str) -> MemberView:
        new_role = _parse_role(role)
        session = self._uow.session
        members = await project_members(session, project_id, for_update=True)
        target = next((row for row in members if row.operator_id == operator_id), None)
        if target is None:
            raise NotFound("Member not found.")
        if new_role is not target.role:
            if Role.OWNER in (new_role, target.role):
                self._require_owner(members)
            if target.role is Role.OWNER and _owner_count(members) == 1:
                raise Conflict("A project must keep at least one owner.")
            await change_member_role(
                session, operator_id=operator_id, project_id=project_id, role=new_role
            )
            await self._record(
                "member.role_changed",
                project_id,
                operator_id,
                {"role": target.role.value},
                {"role": new_role.value},
            )
        updated = await project_members(session, project_id)
        return _view(next(row for row in updated if row.operator_id == operator_id))

    async def remove(self, project_id: UUID, operator_id: UUID) -> None:
        session = self._uow.session
        members = await project_members(session, project_id, for_update=True)
        target = next((row for row in members if row.operator_id == operator_id), None)
        if target is None:
            raise NotFound("Member not found.")
        if target.role is Role.OWNER:
            self._require_owner(members)
            if _owner_count(members) == 1:
                raise Conflict("A project must keep at least one owner.")
        await remove_membership(session, operator_id=operator_id, project_id=project_id)
        await self._record(
            "member.removed", project_id, operator_id, {"role": target.role.value}, None
        )


def _owner_count(members: Sequence[MemberRow]) -> int:
    return sum(1 for row in members if row.role is Role.OWNER)
