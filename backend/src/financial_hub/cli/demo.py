"""``seed-demo``: an idempotent demo workspace for the demo login (docs/DEVELOPMENT.md).

Creates the project "Demo Store" with four operators, one per role; then eight customers, twelve
payments (through the fake provider), one API key, one reconciliation run with an open fee
mismatch, and billing: a usage metric, two prices, one plan, two subscriptions and one finalized
invoice; and an active integration manifest (v1) with a draft v2 that changes a split. Passwords
are random and never shown: the demo login does not need them. Running it
again changes nothing: every record is looked up by its stable key first and created only when
missing.
"""

import secrets
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Final
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from financial_hub.cli.demo_data import (
    DEMO_BILLED_CUSTOMER,
    DEMO_BILLED_SUBSCRIPTION_AGE_DAYS,
    DEMO_CURRENCY,
    DEMO_CUSTOMERS,
    DEMO_FEE_MODEL,
    DEMO_FEE_PRICE,
    DEMO_KEY_NAME,
    DEMO_KEY_SCOPES,
    DEMO_MANIFEST_V1,
    DEMO_MANIFEST_V2,
    DEMO_METRIC_CODE,
    DEMO_METRIC_FIELD,
    DEMO_METRIC_NAME,
    DEMO_PAYMENTS,
    DEMO_PLAN_CODE,
    DEMO_PLAN_NAME,
    DEMO_PRICE_EFFECTIVE_FROM,
    DEMO_TRIAL_CUSTOMER,
    DEMO_TRIAL_DAYS,
    DEMO_USAGE,
    DEMO_USAGE_MODEL,
    DEMO_USAGE_PRICE,
    DUE_DATE_ANCHOR,
)
from financial_hub.modules.audit.application import (
    AuditContext,
    AuditRecorder,
    AuditTarget,
    changed_fields,
)
from financial_hub.modules.billing.application.api import (
    BillingService,
    SubscriptionStatus,
    SubscriptionView,
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
from financial_hub.modules.integrations.application.api import ManifestService
from financial_hub.modules.metering.application.service import MeteringService, UsageItem
from financial_hub.modules.payments.application.api import (
    NewPayment,
    PaymentService,
    ProviderRegistry,
    build_registry,
)
from financial_hub.modules.payments.infrastructure.fake_provider import FakePaymentProvider
from financial_hub.modules.pricing.application.api import PriceCatalogService
from financial_hub.modules.projects.application.api import active_provider_accounts
from financial_hub.modules.projects.application.api_keys import (
    OPERATOR_PRINCIPAL,
    ApiKeyService,
)
from financial_hub.modules.projects.application.service import ProjectService
from financial_hub.modules.projects.domain.api_keys import KeyEnvironment
from financial_hub.modules.reconciliation.application.api import build_reconciliation_service
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
# The seeded reconciliation run: a fixed, past day so re-seeding finds the same run (W8.1).
DEMO_RECONCILIATION_DAY: Final = date(2026, 10, 1)
DEMO_FEE_MISMATCH_PAYMENT: Final = "demo-pay-02"
DEMO_FEE_MISMATCH_EXTRA_FEE: Final = 99


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
    reconciliation_run_created: bool = False
    billing_created: int = 0  # metric, prices, plan, subscriptions and invoices
    invoices_present: int = 0
    manifests_created: int = 0  # integration manifest versions (v1 active, v2 draft)

    @property
    def changed(self) -> bool:
        return (
            self.project_created
            or self.operators_created > 0
            or self.memberships_created > 0
            or self.customers_created > 0
            or self.payments_created > 0
            or self.api_key_created
            or self.reconciliation_run_created
            or self.billing_created > 0
            or self.manifests_created > 0
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
        # Carla (finance) is the second pair of eyes on the manifest Ana writes.
        approver = ProjectScope(
            project_id=project_id,
            principal_kind=OPERATOR_PRINCIPAL,
            principal_id=format_id(IdPrefix.OPERATOR, operator_ids[DEMO_OPERATORS[2].email]),
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
    reconciliation_run_created = await _seed_reconciliation(
        session_factory, scope, clock=clock, services=services
    )
    billing_created, invoices_present = await _seed_billing(
        session_factory, scope, clock=clock, services=services, audit=audit
    )
    manifests_created = await _seed_manifests(
        session_factory, scope, approver, clock=clock, services=services, audit=audit
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
        reconciliation_run_created=reconciliation_run_created,
        billing_created=billing_created,
        invoices_present=invoices_present,
        manifests_created=manifests_created,
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


async def _seed_reconciliation(
    session_factory: async_sessionmaker[AsyncSession],
    scope: ProjectScope,
    *,
    clock: Clock,
    services: DemoServices,
) -> bool:
    """One completed transactions run with one open ``fee_mismatch`` (docs/RECONCILIATION.md).

    The provider side is scripted (the fake keeps no fee history): it reports a net
    ``DEMO_FEE_MISMATCH_EXTRA_FEE`` minor units below what the hub booked for one settled payment.
    """
    payments = PaymentService(session_factory, clock=clock, registry=services.registry)
    provider_payment_id = await _provider_payment_id(
        payments, scope.project_id, DEMO_FEE_MISMATCH_PAYMENT
    )
    if provider_payment_id is None:
        raise DemoSeedError(f"{DEMO_FEE_MISMATCH_PAYMENT}: no provider payment to reconcile")
    reconciliation = build_reconciliation_service(
        session_factory, clock=clock, registry=services.registry
    )
    _, created = await reconciliation.record_demo_fee_mismatch(
        scope.project_id,
        day=DEMO_RECONCILIATION_DAY,
        provider_payment_id=provider_payment_id,
        extra_fee=DEMO_FEE_MISMATCH_EXTRA_FEE,
    )
    return created


async def _provider_payment_id(
    payments: PaymentService, project_id: UUID, external_ref: str
) -> str | None:
    cursor: str | None = None
    while True:
        page = await payments.list_payments(project_id, PageParams(cursor=cursor, limit=PAGE_SIZE))
        for view in page.data:
            if view.external_ref == external_ref:
                return view.provider_payment_id
        if not page.has_more or page.next_cursor is None:
            return None
        cursor = page.next_cursor


async def _seed_billing(
    session_factory: async_sessionmaker[AsyncSession],
    scope: ProjectScope,
    *,
    clock: Clock,
    services: DemoServices,
    audit: AuditContext,
) -> tuple[int, int]:
    """(created, invoices) for the demo billing records (W5.3), each matched by its stable key.

    A metric and its usage, two prices, one plan, a subscription whose first period is closed into
    one finalized invoice (its payment through the fake provider) and one in its trial.
    """
    created = 0
    async with UnitOfWork(session_factory) as uow:
        metering = MeteringService(uow, clock=clock, audit=audit)
        try:
            await metering.get_metric(scope.project_id, DEMO_METRIC_CODE)
        except NotFound:
            await metering.create_metric(
                scope,
                code=DEMO_METRIC_CODE,
                name=DEMO_METRIC_NAME,
                description=None,
                aggregation="sum",
                field=DEMO_METRIC_FIELD,
            )
            created += 1
        catalog = PriceCatalogService(uow, clock=clock, audit=audit)
        effective_from = datetime.combine(DEMO_PRICE_EFFECTIVE_FROM, time(), tzinfo=UTC)
        for code, name, model, metric in (
            (DEMO_FEE_PRICE, "Mensalidade", DEMO_FEE_MODEL, None),
            (DEMO_USAGE_PRICE, "Chamadas de API", DEMO_USAGE_MODEL, DEMO_METRIC_CODE),
        ):
            try:
                await catalog.get_price(scope.project_id, code)
            except NotFound:
                await catalog.create_price(
                    scope,
                    code=code,
                    name=name,
                    currency=DEMO_CURRENCY,
                    model=model,
                    metric_code=metric,
                    effective_from=effective_from,
                )
                created += 1
        customers = CustomerService(
            uow, clock=clock, keyring=services.keyring, pepper=services.pepper, audit=audit
        )
        billed_customer = (await customers.get_by_ref(scope.project_id, DEMO_BILLED_CUSTOMER)).id
        trial_customer = (await customers.get_by_ref(scope.project_id, DEMO_TRIAL_CUSTOMER)).id

    billing = BillingService(
        session_factory,
        clock=clock,
        payments=PaymentService(
            session_factory, clock=clock, registry=services.registry, audit=audit
        ),
        audit=audit,
    )
    try:
        await billing.get_plan(scope.project_id, DEMO_PLAN_CODE)
    except NotFound:
        await billing.create_plan(
            scope,
            code=DEMO_PLAN_CODE,
            name=DEMO_PLAN_NAME,
            price_codes=[DEMO_FEE_PRICE, DEMO_USAGE_PRICE],
            currency=DEMO_CURRENCY,
        )
        created += 1

    billed = await _live_subscription(billing, scope.project_id, billed_customer)
    if billed is None:
        started_at = clock.now() - timedelta(days=DEMO_BILLED_SUBSCRIPTION_AGE_DAYS)
        billed = await billing.create_subscription(
            scope, customer_id=billed_customer, plan_code=DEMO_PLAN_CODE, started_at=started_at
        )
        created += 1
        await _seed_usage(session_factory, scope, clock=clock, audit=audit)
    billed_id = billed.subscription.id
    invoices = await billing.list_invoices(
        scope.project_id, PageParams(limit=PAGE_SIZE), subscription_id=billed_id
    )
    if not invoices.data:
        # closes every period that has ended: the first one, into a finalized invoice
        created += len(await billing.close_subscription(scope.project_id, billed_id))
        invoices = await billing.list_invoices(
            scope.project_id, PageParams(limit=PAGE_SIZE), subscription_id=billed_id
        )
    if await _live_subscription(billing, scope.project_id, trial_customer) is None:
        await billing.create_subscription(
            scope,
            customer_id=trial_customer,
            plan_code=DEMO_PLAN_CODE,
            trial_days=DEMO_TRIAL_DAYS,
        )
        created += 1
    return created, len(invoices.data)


async def _live_subscription(
    billing: BillingService, project_id: UUID, customer_id: str
) -> SubscriptionView | None:
    page = await billing.list_subscriptions(
        project_id, PageParams(limit=PAGE_SIZE), customer_id=customer_id
    )
    for view in page.data:
        if (
            view.plan_code == DEMO_PLAN_CODE
            and view.subscription.status is not SubscriptionStatus.CANCELED
        ):
            return view
    return None


async def _seed_usage(
    session_factory: async_sessionmaker[AsyncSession],
    scope: ProjectScope,
    *,
    clock: Clock,
    audit: AuditContext,
) -> None:
    """The billed customer's API calls, only when its subscription is new (timestamps move)."""
    now = clock.now()
    async with UnitOfWork(session_factory) as uow:
        metering = MeteringService(uow, clock=clock, audit=audit)
        for position, (days_ago, calls) in enumerate(DEMO_USAGE, start=1):
            await metering.ingest(
                scope.project_id,
                UsageItem(
                    transaction_id=f"demo-usage-{position:02d}-{now.date().isoformat()}",
                    metric=DEMO_METRIC_CODE,
                    properties={DEMO_METRIC_FIELD: calls},
                    timestamp=now - timedelta(days=days_ago),
                    customer_external_ref=DEMO_BILLED_CUSTOMER,
                ),
            )


async def _seed_manifests(
    session_factory: async_sessionmaker[AsyncSession],
    author: ProjectScope,
    approver: ProjectScope,
    *,
    clock: Clock,
    services: DemoServices,
    audit: AuditContext,
) -> int:
    """Manifest versions created (W6.1): v1 written by Ana, approved and activated by Carla (one
    transaction: all or nothing), then v2 as a draft. Matched by version number."""
    created = 0
    for version, content in ((1, DEMO_MANIFEST_V1), (2, DEMO_MANIFEST_V2)):
        async with UnitOfWork(session_factory) as uow:
            manifests = ManifestService(uow, clock=clock, audit=audit, keyring=services.keyring)
            try:
                await manifests.get(author.project_id, version)
                continue
            except NotFound:
                await manifests.create(author, content)
            created += 1
            if version == 1:
                validated = await manifests.validate(author, version)
                await manifests.approve(approver, version, content_hash=validated.content_hash)
                await manifests.activate(approver, version)
    return created
