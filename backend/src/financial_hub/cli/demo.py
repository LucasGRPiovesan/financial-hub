"""``seed-demo``: an idempotent demo workspace for the demo login (docs/DEVELOPMENT.md).

Creates the project "Demo Store" with four operators, one per role; then eight customers, twelve
payments (through the fake provider) and one API key. Passwords are random and never shown: the
demo login does not need them. Running it again changes nothing: every record is looked up by its
stable key first and created only when missing.
"""

import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Final
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from financial_hub.cli.demo_data import (
    DEMO_CURRENCY,
    DEMO_CUSTOMERS,
    DEMO_KEY_NAME,
    DEMO_KEY_SCOPES,
    DEMO_PAYMENTS,
    DUE_DATE_ANCHOR,
)
from financial_hub.modules.audit.application import (
    AuditContext,
    AuditRecorder,
    AuditTarget,
    changed_fields,
)
from financial_hub.modules.customers.application.service import CustomerService
from financial_hub.modules.identity.application.api import (
    DEMO_EMAIL_DOMAIN,
    Permission,
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
from financial_hub.modules.payments.application.api import (
    NewPayment,
    PaymentService,
    ProviderRegistry,
    build_registry,
)
from financial_hub.modules.payments.infrastructure.fake_provider import FakePaymentProvider
from financial_hub.modules.projects.application.api import active_provider_accounts
from financial_hub.modules.projects.application.api_keys import (
    OPERATOR_PRINCIPAL,
    ApiKeyService,
)
from financial_hub.modules.projects.application.service import ProjectService
from financial_hub.modules.projects.domain.api_keys import KeyEnvironment
from financial_hub.platform.crypto.keyring import Keyring
from financial_hub.platform.db import UnitOfWork
from financial_hub.platform.http.auth import ProjectScope
from financial_hub.platform.settings import Settings, get_settings
from financial_hub.shared.clock import Clock, SystemClock
from financial_hub.shared.errors import NotFound
from financial_hub.shared.ids import IdPrefix, format_id, parse_id
from financial_hub.shared.pagination import PageParams

DEMO_PROJECT_SLUG: Final = "demo-store"
DEMO_PROJECT_NAME: Final = "Demo Store"
PAGE_SIZE: Final = 100


class DemoSeedError(RuntimeError):
    """The workspace cannot be seeded as it is (printed as an error, nothing is written)."""


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
class DemoServices:
    """What the demo records are written with: the same wiring the web process uses (main.py)."""

    registry: ProviderRegistry
    keyring: Keyring
    pepper: bytes
    key_environment: KeyEnvironment

    @classmethod
    def from_settings(cls, settings: Settings) -> DemoServices:
        return cls(
            registry=build_registry(settings),
            keyring=Keyring.from_settings(settings),
            pepper=settings.api_key_pepper.get_secret_value().encode(),
            key_environment=settings.api_key_environment,
        )


@dataclass(frozen=True, slots=True)
class SeedResult:
    project_created: bool
    operators_created: int
    memberships_created: int
    customers_created: int
    payments_created: int
    api_key_created: bool
    # how many of each demo record exist once the seed has run (created or already there)
    customers_present: int
    payments_present: int
    api_keys_present: int

    @property
    def changed(self) -> bool:
        return (
            self.project_created
            or self.operators_created > 0
            or self.memberships_created > 0
            or self.customers_created > 0
            or self.payments_created > 0
            or self.api_key_created
        )


def _fake_provider(services: DemoServices) -> FakePaymentProvider:
    """The fake provider the payments are scripted on; the seed never reaches a real provider."""
    fake = services.registry.fake
    if not isinstance(fake, FakePaymentProvider):
        raise DemoSeedError(
            "the fake payment provider is disabled (payments_fake_provider_enabled): "
            "seed-demo creates its payments with it"
        )
    return fake


async def seed_demo(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    hasher: PasswordHasher | None = None,
    clock: Clock | None = None,
    services: DemoServices | None = None,
) -> SeedResult:
    """Create whatever of the demo workspace is missing (one transaction per service call)."""
    hasher = hasher or default_password_hasher()
    clock = clock or SystemClock()
    services = services or DemoServices.from_settings(get_settings())
    _fake_provider(services)  # refuse before writing anything
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

        # The demo writes act as the owner: the services need an operator principal, and the
        # system permission set stands in for the owner's role (this is a dev-only seed).
        scope = ProjectScope(
            project_id=project_id,
            principal_kind=OPERATOR_PRINCIPAL,
            principal_id=format_id(IdPrefix.OPERATOR, operator_ids[DEMO_OPERATORS[0].email]),
            permissions=frozenset(Permission),
        )
        customers_created, customers_present = await _seed_customers(
            uow, scope, clock=clock, services=services, audit=audit
        )

    payments_created, payments_present = await _seed_payments(
        session_factory, scope, clock=clock, services=services, audit=audit
    )
    api_key_created, api_keys_present = await _seed_api_key(
        session_factory, scope, clock=clock, services=services, audit=audit
    )
    return SeedResult(
        project_created=project_created,
        operators_created=operators_created,
        memberships_created=memberships_created,
        customers_created=customers_created,
        payments_created=payments_created,
        api_key_created=api_key_created,
        customers_present=customers_present,
        payments_present=payments_present,
        api_keys_present=api_keys_present,
    )


async def _seed_customers(
    uow: UnitOfWork,
    scope: ProjectScope,
    *,
    clock: Clock,
    services: DemoServices,
    audit: AuditContext,
) -> tuple[int, int]:
    """(created, present) for the demo customers, matched by their ``external_ref``."""
    customers = CustomerService(
        uow, clock=clock, keyring=services.keyring, pepper=services.pepper, audit=audit
    )
    created = 0
    for spec in DEMO_CUSTOMERS:
        try:
            await customers.get_by_ref(scope.project_id, spec.external_ref)
        except NotFound:
            await customers.upsert(
                scope,
                spec.external_ref,
                name=spec.name,
                kind=spec.kind,
                email=spec.email,
                phone=spec.phone,
                document=None,  # optional; the demo never stores a CPF or CNPJ
                metadata={},
            )
            created += 1
    return created, len(DEMO_CUSTOMERS)


async def _seed_payments(
    session_factory: async_sessionmaker[AsyncSession],
    scope: ProjectScope,
    *,
    clock: Clock,
    services: DemoServices,
    audit: AuditContext,
) -> tuple[int, int]:
    """(created, present) for the demo payments, matched by their ``external_ref``."""
    fake = _fake_provider(services)
    async with UnitOfWork(session_factory) as uow:
        if await active_provider_accounts(uow.session, scope.project_id):
            # With a real account the payments service would route to that provider.
            raise DemoSeedError("Demo Store has an active provider account: seed-demo needs none")
    payments = PaymentService(session_factory, clock=clock, registry=services.registry, audit=audit)
    existing = await _existing_payment_refs(payments, scope.project_id)
    created = 0
    for spec in DEMO_PAYMENTS:
        if spec.external_ref in existing:
            continue
        # the fake provider answers the next create with this behaviour
        fake.script(spec.behavior)
        view = await payments.create(
            scope,
            NewPayment(
                amount=spec.amount,
                currency=DEMO_CURRENCY,
                method=spec.method,
                due_date=DUE_DATE_ANCHOR + timedelta(days=spec.due_in_days),
                customer_external_ref=spec.customer,
                external_ref=spec.external_ref,
                metadata={"origin": "seed-demo"},
            ),
        )
        if spec.settle:
            if view.provider_payment_id is None:
                raise DemoSeedError(f"{spec.external_ref}: the provider created no payment")
            fake.settle(view.provider_payment_id)
            await payments.refresh(parse_id(view.id, IdPrefix.PAYMENT_INTENT))
        if spec.partial_refund is not None:
            await payments.refund(
                scope, view.id, amount=spec.partial_refund, reason="Reembolso parcial (demo)"
            )
        if spec.full_refund:
            await payments.refund(scope, view.id, amount=None, reason="Reembolso total (demo)")
        created += 1
    present = sum(1 for spec in DEMO_PAYMENTS if spec.external_ref in existing) + created
    return created, present


async def _existing_payment_refs(payments: PaymentService, project_id: UUID) -> set[str]:
    refs: set[str] = set()
    cursor: str | None = None
    while True:
        page = await payments.list_payments(project_id, PageParams(cursor=cursor, limit=PAGE_SIZE))
        refs.update(view.external_ref for view in page.data if view.external_ref is not None)
        if not page.has_more or page.next_cursor is None:
            return refs
        cursor = page.next_cursor


async def _seed_api_key(
    session_factory: async_sessionmaker[AsyncSession],
    scope: ProjectScope,
    *,
    clock: Clock,
    services: DemoServices,
    audit: AuditContext,
) -> tuple[bool, int]:
    """(created, present): one active key named ``DEMO_KEY_NAME``. The secret is never printed."""
    async with UnitOfWork(session_factory) as uow:
        keys = ApiKeyService(
            uow,
            clock=clock,
            pepper=services.pepper,
            environment=services.key_environment,
            audit=audit,
        )
        if await _has_active_demo_key(keys, scope.project_id):
            return False, 1
        await keys.create(scope, name=DEMO_KEY_NAME, scopes=list(DEMO_KEY_SCOPES), expires_at=None)
    return True, 1


async def _has_active_demo_key(keys: ApiKeyService, project_id: UUID) -> bool:
    cursor: str | None = None
    while True:
        page = await keys.list_keys(project_id, PageParams(cursor=cursor, limit=PAGE_SIZE))
        if any(view.name == DEMO_KEY_NAME and view.status == "active" for view in page.data):
            return True
        if not page.has_more or page.next_cursor is None:
            return False
        cursor = page.next_cursor
