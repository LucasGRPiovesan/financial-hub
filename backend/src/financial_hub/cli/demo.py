"""``seed-demo``: an idempotent demo workspace for the demo login (docs/DEVELOPMENT.md).

Creates the project "Demo Store" and four operators, one per role. Passwords are random and never
shown: the demo login does not need them. Running it again changes nothing.
"""

import secrets
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from financial_hub.modules.audit.application import (
    AuditContext,
    AuditRecorder,
    AuditTarget,
    changed_fields,
)
from financial_hub.modules.identity.application.api import (
    DEMO_EMAIL_DOMAIN,
    Role,
    find_operator_by_email,
    grant_membership,
    project_members,
)
from financial_hub.modules.identity.application.service import (
    PasswordHasher,
    create_operator,
    default_password_hasher,
)
from financial_hub.modules.projects.application.service import ProjectService
from financial_hub.platform.db import UnitOfWork
from financial_hub.shared.clock import Clock, SystemClock
from financial_hub.shared.ids import IdPrefix, format_id, parse_id

DEMO_PROJECT_SLUG: Final = "demo-store"
DEMO_PROJECT_NAME: Final = "Demo Store"


@dataclass(frozen=True, slots=True)
class DemoOperator:
    email: str
    display_name: str
    role: Role


DEMO_OPERATORS: Final = (
    DemoOperator(f"ana@{DEMO_EMAIL_DOMAIN}", "Ana Owner", Role.OWNER),
    DemoOperator(f"bruno@{DEMO_EMAIL_DOMAIN}", "Bruno Admin", Role.ADMIN),
    DemoOperator(f"carla@{DEMO_EMAIL_DOMAIN}", "Carla Finance", Role.FINANCE),
    DemoOperator(f"diego@{DEMO_EMAIL_DOMAIN}", "Diego Viewer", Role.VIEWER),
)


@dataclass(frozen=True, slots=True)
class SeedResult:
    project_created: bool
    operators_created: int
    memberships_created: int

    @property
    def changed(self) -> bool:
        return self.project_created or self.operators_created > 0 or self.memberships_created > 0


async def seed_demo(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    hasher: PasswordHasher | None = None,
    clock: Clock | None = None,
) -> SeedResult:
    """Create whatever of the demo workspace is missing, in one transaction."""
    hasher = hasher or default_password_hasher()
    clock = clock or SystemClock()
    audit = AuditContext.system()
    operators_created = 0
    memberships_created = 0
    async with UnitOfWork(session_factory) as uow:
        session = uow.session
        operator_ids: dict[str, UUID] = {}
        for spec in DEMO_OPERATORS:
            existing = await find_operator_by_email(session, spec.email)
            if existing is not None:
                operator_ids[spec.email] = existing.id
                continue
            created = await create_operator(
                uow,
                hasher=hasher,
                clock=clock,
                email=spec.email,
                display_name=spec.display_name,
                password=secrets.token_urlsafe(32),
            )
            operator_ids[spec.email] = parse_id(created.id, IdPrefix.OPERATOR)
            operators_created += 1

        projects = ProjectService(uow, audit)
        project = await projects.find_by_slug(DEMO_PROJECT_SLUG)
        project_created = project is None
        if project is None:
            project = await projects.create(
                owner_operator_id=operator_ids[DEMO_OPERATORS[0].email],
                slug=DEMO_PROJECT_SLUG,
                name=DEMO_PROJECT_NAME,
            )
            memberships_created += 1  # the owner, granted by project creation
        project_id = parse_id(project.id, IdPrefix.PROJECT)

        members = {row.operator_id for row in await project_members(session, project_id)}
        for spec in DEMO_OPERATORS:
            operator_id = operator_ids[spec.email]
            if operator_id in members:
                continue
            await grant_membership(
                session, operator_id=operator_id, project_id=project_id, role=spec.role
            )
            await AuditRecorder.record(
                session,
                actor=audit,
                action="member.added",
                target=AuditTarget("operator", format_id(IdPrefix.OPERATOR, operator_id)),
                diff=changed_fields(None, {"role": spec.role.value}),
                project_id=project_id,
            )
            memberships_created += 1
    return SeedResult(
        project_created=project_created,
        operators_created=operators_created,
        memberships_created=memberships_created,
    )
