"""Plans, subscriptions and invoices (docs/DOMAIN.md "Billing", docs/FINANCIAL_MODEL.md R1/R11).

Money rules:

- Every amount is an integer of minor units. Lines are rated by :class:`PricingService` (the
  version effective at the period start, ``ROUNDING_POLICY`` pinned on the line) over the
  metering aggregation of the period; the invoice total is the integer sum of its lines.
- **Finalize** books R1 (``invoice:{id}:finalized``, ``u = 0``) and opens the one payment intent
  of ``amount_due = total - credits_applied`` in the same transaction; the provider call is sent
  after commit. ``credits_applied`` is always 0 until the wallet (W5.4). Nothing due: the invoice
  is paid at once and no intent exists. A zero total books no R1 (an entry needs an amount).
- **Paid** follows the capture of that intent (``payment.status_changed``, idempotent); the
  payments ledger hook books R2/R3 for it and never a second R1.
- **Void** (R11) is refused once money was captured: it cancels the intent and reverses R1.

Period close (the ``billing.close_due`` schedule) closes each ended period in its own
transaction (one ledger write each), idempotent by ``(subscription, period_start)``: the
subscription row is locked and re-checked, and the invoice insert ignores a duplicate period.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from financial_hub.modules.audit.application import (
    Actor,
    AuditContext,
    AuditRecorder,
    AuditTarget,
)
from financial_hub.modules.billing.application import events as billing_events
from financial_hub.modules.billing.domain import (
    DEFAULT_DAYS_UNTIL_DUE,
    DEFAULT_PAYMENT_METHOD,
    MAX_ANCHOR_DAY,
    MAX_DAYS_UNTIL_DUE,
    MAX_PLAN_PRICES,
    MAX_TRIAL_DAYS,
    MIN_ANCHOR_DAY,
    PAYMENT_METHODS,
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    LineKind,
    Period,
    Plan,
    Subscription,
    SubscriptionStatus,
    anchor_day_of,
    can_move,
    full_period,
    invoice_amounts,
    period_from,
)
from financial_hub.modules.billing.infrastructure import repository
from financial_hub.modules.customers.application.api import resolve_customer_identity
from financial_hub.modules.events.application import EventDraft, EventPublisher
from financial_hub.modules.ledger.application import (
    CustomerId,
    InvoiceFinalized,
    InvoiceVoided,
    RecipeContext,
    apply_recipes,
)
from financial_hub.modules.metering.application.api import Aggregation, aggregate_usage
from financial_hub.modules.payments.application.api import PaymentService, is_captured
from financial_hub.modules.pricing.application.api import (
    AggregatedUsage,
    BillingPeriod,
    CatalogEntry,
    PricingService,
    Proration,
    RatedCharge,
    catalog_entries,
    catalog_entries_by_code,
    operator_only,
    price_book,
)
from financial_hub.platform.db import UnitOfWork
from financial_hub.platform.http.auth import ProjectScope
from financial_hub.shared.clock import Clock
from financial_hub.shared.errors import Conflict, FieldError, NotFound, ValidationFailed
from financial_hub.shared.ids import IdPrefix, format_id, new_uuid
from financial_hub.shared.money import Currency, Money
from financial_hub.shared.pagination import Cursor, Page, PageParams, build_page

logger = structlog.get_logger()

PLAN_CODE: Final = re.compile(r"[a-z0-9][a-z0-9_.-]{0,99}")
NAME_MAX_LENGTH: Final = 200
# Usage summed from the billing start: late events land on the next invoice (docs/DOMAIN.md).
CUMULATIVE: Final = frozenset({Aggregation.SUM, Aggregation.COUNT})
# A subscription far behind is caught up a bounded number of periods per call.
MAX_CATCH_UP_PERIODS: Final = 24
_LIVE_INDEX: Final = "uq_subscriptions_live"


class PlanCodeTaken(Conflict):
    """The project already has a plan with this code (409)."""

    slug = "plan-code-taken"
    title = "Plan code already in use"


class AlreadySubscribed(Conflict):
    """The customer already has a live subscription to the plan (409)."""

    slug = "already-subscribed"
    title = "Customer already subscribed"


@dataclass(frozen=True, slots=True)
class PlanView:
    plan: Plan
    price_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SubscriptionView:
    subscription: Subscription
    plan_code: str


def _invalid(pointer: str, detail: str) -> ValidationFailed:
    return ValidationFailed(
        "The request contains invalid fields.", errors=[FieldError(pointer=pointer, detail=detail)]
    )


def _proration(period: Period, anchor_day: int) -> Proration | None:
    """Share of the anchor period ``period`` covers (``None`` when it is a whole one)."""
    full = full_period(period.start, anchor_day)
    proration = Proration.for_period(BillingPeriod(full.start, full.end), period.start, period.end)
    return None if proration.is_full else proration


def _line_details(
    rated: RatedCharge, proration: Proration | None, usage: dict[str, Any] | None
) -> dict[str, Any]:
    details: dict[str, Any] = {
        "lines": [
            {
                "key": line.key,
                "quantity": str(line.quantity),
                "unit_amount": str(line.unit_amount),
                "amount": line.amount.minor_units,
                "metadata": dict(line.metadata),
            }
            for line in rated.lines
        ],
        "proration": (
            None
            if proration is None
            else {"active_days": proration.active_days, "period_days": proration.period_days}
        ),
    }
    if usage is not None:
        details["usage"] = usage
    return details


class BillingService:
    """Owns its transactions (like :class:`PaymentService`); the worker handler and the close use
    the same service. ``payments`` opens and cancels the invoices' payment intents."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Clock,
        payments: PaymentService,
        audit: AuditContext | None = None,
        publisher: EventPublisher | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._payments = payments
        self._audit = audit or AuditContext.system()
        self._publisher = publisher or EventPublisher(clock=clock, notifier=None)

    def _uow(self) -> UnitOfWork:
        return UnitOfWork(self._session_factory)

    # --- helpers --------------------------------------------------------------------------------

    async def _record(
        self, uow: UnitOfWork, scope: ProjectScope, action: str, target: AuditTarget, diff: Any
    ) -> None:
        await AuditRecorder.record(
            uow.session,
            actor=self._audit.acting(Actor.of_principal(scope.principal_kind, scope.principal_id)),
            action=action,
            target=target,
            diff=diff,
            project_id=scope.project_id,
        )

    async def _invoice_event(self, uow: UnitOfWork, event_type: str, invoice: Invoice) -> None:
        public_id = format_id(IdPrefix.INVOICE, invoice.id)
        await self._publisher.publish(
            uow,
            EventDraft(
                type=event_type,
                data=billing_events.InvoicePayload(
                    invoice_id=public_id,
                    subscription_id=format_id(IdPrefix.SUBSCRIPTION, invoice.subscription_id),
                    customer_id=format_id(IdPrefix.CUSTOMER, invoice.customer_id),
                    status=invoice.status.value,
                ),
                project_id=invoice.project_id,
                subject=public_id,
                correlation_id=invoice.id,
            ),
        )

    async def _subscription_event(
        self, uow: UnitOfWork, event_type: str, subscription: Subscription, plan_code: str
    ) -> None:
        public_id = format_id(IdPrefix.SUBSCRIPTION, subscription.id)
        await self._publisher.publish(
            uow,
            EventDraft(
                type=event_type,
                data=billing_events.SubscriptionPayload(
                    subscription_id=public_id,
                    customer_id=format_id(IdPrefix.CUSTOMER, subscription.customer_id),
                    plan=plan_code,
                    status=subscription.status.value,
                ),
                project_id=subscription.project_id,
                subject=public_id,
                correlation_id=subscription.id,
            ),
        )

    async def _plan_of(self, session: AsyncSession, subscription: Subscription) -> Plan:
        plans = await repository.get_plans(session, subscription.project_id, [subscription.plan_id])
        if not plans:
            raise NotFound("Plan not found.")
        return plans[0]

    async def _plan_entries(self, session: AsyncSession, plan: Plan) -> list[CatalogEntry]:
        by_id = {
            entry.price.id: entry
            for entry in await catalog_entries(session, plan.project_id, plan.price_ids)
        }
        return [by_id[price_id] for price_id in plan.price_ids]

    async def _plan_view(self, session: AsyncSession, plan: Plan) -> PlanView:
        entries = await self._plan_entries(session, plan)
        return PlanView(plan, tuple(entry.price.code for entry in entries))

    # --- plans ----------------------------------------------------------------------------------

    async def create_plan(
        self,
        scope: ProjectScope,
        *,
        code: str,
        name: str,
        price_codes: Sequence[str],
        currency: str = "BRL",
    ) -> PlanView:
        """A plan bills its prices every month; they share the plan's currency."""
        operator_only(scope)
        if PLAN_CODE.fullmatch(code) is None:
            raise _invalid("/code", "lowercase letters, digits and `_ . -`, at most 100")
        clean_name = name.strip()
        if not 1 <= len(clean_name) <= NAME_MAX_LENGTH:
            raise _invalid("/name", f"must be 1-{NAME_MAX_LENGTH} characters")
        try:
            plan_currency = Currency.of(currency)
        except ValueError:
            raise _invalid("/currency", "unknown ISO 4217 currency") from None
        codes = list(price_codes)
        if not 1 <= len(codes) <= MAX_PLAN_PRICES or len(set(codes)) != len(codes):
            raise _invalid("/prices", f"1-{MAX_PLAN_PRICES} distinct price codes")
        async with self._uow() as uow:
            by_code = {
                entry.price.code: entry
                for entry in await catalog_entries_by_code(uow.session, scope.project_id, codes)
            }
            for index, price_code in enumerate(codes):
                entry = by_code.get(price_code)
                pointer = f"/prices/{index}"
                if entry is None:
                    raise _invalid(pointer, "no price with this code")
                if entry.price.currency != plan_currency:
                    raise _invalid(pointer, f"the price is not in {plan_currency.code}")
                latest = entry.price.latest
                if latest is not None and latest.model.kind == "percentage":
                    raise _invalid(pointer, "percentage prices cannot be part of a plan")
            plan = Plan(
                id=new_uuid(),
                project_id=scope.project_id,
                code=code,
                name=clean_name,
                currency=plan_currency.code,
                price_ids=tuple(by_code[price_code].price.id for price_code in codes),
                created_at=self._clock.now(),
            )
            if not await repository.insert_plan(uow.session, plan):
                raise PlanCodeTaken(f"The project already has a plan with code {code}.")
            await self._record(
                uow,
                scope,
                "plan.created",
                AuditTarget("plan", code),
                {"currency": plan.currency, "prices": codes},
            )
        return PlanView(plan, tuple(codes))

    async def get_plan(self, project_id: UUID, code: str) -> PlanView:
        async with self._uow() as uow:
            plan = await repository.get_plan(uow.session, project_id, code)
            if plan is None:
                raise NotFound("Plan not found.")
            return await self._plan_view(uow.session, plan)

    async def list_plans(self, project_id: UUID, params: PageParams) -> Page[PlanView]:
        async with self._uow() as uow:
            rows = await repository.list_plans(
                uow.session, project_id, cursor=params.decoded_cursor(), limit=params.limit + 1
            )
            views = {plan.id: await self._plan_view(uow.session, plan) for plan in rows}
        return build_page(
            rows,
            params.limit,
            lambda plan: views[plan.id],
            lambda plan: Cursor(created_at=plan.created_at, id=plan.id),
        )

    # --- subscriptions --------------------------------------------------------------------------

    async def create_subscription(
        self,
        scope: ProjectScope,
        *,
        customer_id: str,
        plan_code: str,
        trial_days: int = 0,
        anchor_day: int | None = None,
        payment_method: str = DEFAULT_PAYMENT_METHOD,
        days_until_due: int = DEFAULT_DAYS_UNTIL_DUE,
        started_at: datetime | None = None,
    ) -> SubscriptionView:
        """Subscribe a customer to a plan from now (``started_at`` is for the demo seed only).

        With a trial the first period is the trial (never invoiced); paid service starts at its
        end. The anchor day defaults to the day paid service starts.
        """
        if not 0 <= trial_days <= MAX_TRIAL_DAYS:
            raise _invalid("/trial_days", f"must be 0-{MAX_TRIAL_DAYS}")
        if anchor_day is not None and not MIN_ANCHOR_DAY <= anchor_day <= MAX_ANCHOR_DAY:
            raise _invalid("/anchor_day", f"must be {MIN_ANCHOR_DAY}-{MAX_ANCHOR_DAY}")
        if payment_method not in PAYMENT_METHODS:
            raise _invalid("/payment_method", "must be one of: pix, boleto, card")
        if not 0 <= days_until_due <= MAX_DAYS_UNTIL_DUE:
            raise _invalid("/days_until_due", f"must be 0-{MAX_DAYS_UNTIL_DUE}")
        now = self._clock.now()
        start = now if started_at is None else started_at
        trial_end = start + timedelta(days=trial_days) if trial_days else None
        billing_start = trial_end or start
        anchor = anchor_day_of(billing_start) if anchor_day is None else anchor_day
        first = Period(start, trial_end) if trial_end else period_from(start, anchor)
        async with self._uow() as uow:
            session = uow.session
            identity = await resolve_customer_identity(
                session, project_id=scope.project_id, customer_id=customer_id, external_ref=None
            )
            if identity is None:
                raise NotFound("Customer not found.")
            plan = await repository.get_plan(session, scope.project_id, plan_code)
            if plan is None:
                raise NotFound("Plan not found.")
            for entry in await self._plan_entries(session, plan):
                try:
                    entry.price.version_at(billing_start)
                except NotFound:
                    raise Conflict(
                        f"Price {entry.price.code} has no version in effect when billing starts."
                    ) from None
            if await repository.find_live_subscription(
                session, scope.project_id, identity, plan.id
            ):
                raise AlreadySubscribed("The customer already has a live subscription to it.")
            subscription = Subscription(
                id=new_uuid(),
                project_id=scope.project_id,
                customer_id=identity,
                plan_id=plan.id,
                status=SubscriptionStatus.TRIALING if trial_end else SubscriptionStatus.ACTIVE,
                anchor_day=anchor,
                started_at=start,
                trial_end=trial_end,
                current_period_start=first.start,
                current_period_end=first.end,
                cancel_at_period_end=False,
                canceled_at=None,
                ended_at=None,
                payment_method=payment_method,
                days_until_due=days_until_due,
                created_at=now,
                updated_at=now,
            )
            try:
                await repository.insert_subscription(session, subscription)
            except IntegrityError as error:
                if _LIVE_INDEX in str(error.orig):
                    raise AlreadySubscribed(
                        "The customer already has a live subscription to it."
                    ) from None
                raise
            await self._record(
                uow,
                scope,
                "subscription.created",
                AuditTarget("subscription", format_id(IdPrefix.SUBSCRIPTION, subscription.id)),
                {"plan": plan.code, "trial_days": trial_days, "anchor_day": anchor},
            )
            await self._subscription_event(
                uow, billing_events.SUBSCRIPTION_CREATED.name, subscription, plan.code
            )
        return SubscriptionView(subscription, plan.code)

    async def get_subscription(self, project_id: UUID, subscription_id: UUID) -> SubscriptionView:
        async with self._uow() as uow:
            subscription = await repository.get_subscription(
                uow.session, project_id, subscription_id
            )
            if subscription is None:
                raise NotFound("Subscription not found.")
            plan = await self._plan_of(uow.session, subscription)
        return SubscriptionView(subscription, plan.code)

    async def list_subscriptions(
        self, project_id: UUID, params: PageParams, *, customer_id: str | None = None
    ) -> Page[SubscriptionView]:
        async with self._uow() as uow:
            identity: UUID | None = None
            if customer_id is not None:
                identity = await resolve_customer_identity(
                    uow.session, project_id=project_id, customer_id=customer_id, external_ref=None
                )
                if identity is None:
                    return Page[SubscriptionView](data=[], next_cursor=None, has_more=False)
            rows = await repository.list_subscriptions(
                uow.session,
                project_id,
                cursor=params.decoded_cursor(),
                limit=params.limit + 1,
                customer_id=identity,
            )
            plans = await repository.get_plans(
                uow.session, project_id, list({row.plan_id for row in rows})
            )
        codes = {plan.id: plan.code for plan in plans}
        return build_page(
            rows,
            params.limit,
            lambda row: SubscriptionView(row, codes[row.plan_id]),
            lambda row: Cursor(created_at=row.created_at, id=row.id),
        )

    async def cancel_subscription(
        self, scope: ProjectScope, subscription_id: UUID, *, at_period_end: bool
    ) -> SubscriptionView:
        """Cancel at the end of the current period, or now.

        Now: a trial ends without an invoice; an active subscription gets a final invoice for
        ``[current period start, now)`` (fixed fees prorated by day, usage to date), finalized at
        once. Periods that already ended are closed first.
        """
        if not at_period_end:
            await self.close_subscription(scope.project_id, subscription_id)
        async with self._uow() as uow:
            session = uow.session
            subscription = await repository.get_subscription(
                session, scope.project_id, subscription_id, lock=True
            )
            if subscription is None:
                raise NotFound("Subscription not found.")
            if subscription.status is SubscriptionStatus.CANCELED:
                raise Conflict("The subscription is already canceled.")
            plan = await self._plan_of(session, subscription)
            now = self._clock.now()
            if at_period_end:
                if subscription.cancel_at_period_end:
                    return SubscriptionView(subscription, plan.code)
                updated = replace(
                    subscription, cancel_at_period_end=True, canceled_at=now, updated_at=now
                )
                await repository.update_subscription(session, updated)
            else:
                if subscription.current_period_end <= now:
                    raise Conflict("The subscription has a period being closed; retry shortly.")
                period_end = subscription.current_period_end
                if (
                    subscription.status is SubscriptionStatus.ACTIVE
                    and now > subscription.current_period_start
                ):
                    final = Period(subscription.current_period_start, now)
                    invoice, created = await self._draft(uow, subscription, final)
                    if created:
                        await self._finalize(uow, invoice, subscription)
                    period_end = now
                updated = replace(
                    subscription,
                    status=SubscriptionStatus.CANCELED,
                    current_period_end=period_end,
                    cancel_at_period_end=False,
                    canceled_at=now,
                    ended_at=now,
                    updated_at=now,
                )
                await repository.update_subscription(session, updated)
                await self._subscription_event(
                    uow, billing_events.SUBSCRIPTION_CANCELED.name, updated, plan.code
                )
            await self._record(
                uow,
                scope,
                "subscription.canceled",
                AuditTarget("subscription", format_id(IdPrefix.SUBSCRIPTION, subscription.id)),
                {"at_period_end": at_period_end},
            )
        return SubscriptionView(updated, plan.code)

    # --- period close ---------------------------------------------------------------------------

    async def close_due(self, *, limit: int = 100) -> int:
        """Close every ended period (all projects); returns the subscriptions handled.

        A subscription that fails is logged and retried on the next tick; the others go on.
        """
        async with self._uow() as uow:
            due = await repository.due_subscriptions(uow.session, self._clock.now(), limit=limit)
        for project_id, subscription_id in due:
            try:
                await self.close_subscription(project_id, subscription_id)
            except Exception:
                logger.exception(
                    "billing.close_failed",
                    project_id=str(project_id),
                    subscription_id=str(subscription_id),
                )
        return len(due)

    async def close_subscription(self, project_id: UUID, subscription_id: UUID) -> list[UUID]:
        """Close the subscription's ended periods, oldest first; returns the invoices created."""
        created: list[UUID] = []
        for _ in range(MAX_CATCH_UP_PERIODS):
            async with self._uow() as uow:
                closed, invoice_id = await self._close_one(uow, project_id, subscription_id)
            if invoice_id is not None:
                created.append(invoice_id)
            if not closed:
                break
        return created

    async def _close_one(
        self, uow: UnitOfWork, project_id: UUID, subscription_id: UUID
    ) -> tuple[bool, UUID | None]:
        """Close the current period if it ended: ``(advanced, invoice created)``."""
        session = uow.session
        subscription = await repository.get_subscription(
            session, project_id, subscription_id, lock=True
        )
        now = self._clock.now()
        if (
            subscription is None
            or subscription.status is SubscriptionStatus.CANCELED
            or subscription.current_period_end > now
        ):
            return False, None
        period = subscription.current_period
        created_id: UUID | None = None
        if subscription.status is SubscriptionStatus.ACTIVE:
            invoice, created = await self._draft(uow, subscription, period)
            if created:
                created_id = invoice.id
                await self._finalize(uow, invoice, subscription)
        if subscription.cancel_at_period_end:
            updated = replace(
                subscription,
                status=SubscriptionStatus.CANCELED,
                cancel_at_period_end=False,
                ended_at=period.end,
                updated_at=now,
            )
            await repository.update_subscription(session, updated)
            plan = await self._plan_of(session, subscription)
            await self._subscription_event(
                uow, billing_events.SUBSCRIPTION_CANCELED.name, updated, plan.code
            )
            return False, created_id
        following = period_from(period.end, subscription.anchor_day)
        await repository.update_subscription(
            session,
            replace(
                subscription,
                status=SubscriptionStatus.ACTIVE,
                current_period_start=following.start,
                current_period_end=following.end,
                updated_at=now,
            ),
        )
        return True, created_id

    async def _usage(
        self, session: AsyncSession, subscription: Subscription, entry: CatalogEntry, period: Period
    ) -> tuple[Decimal, dict[str, Any]]:
        """The quantity to bill for a usage price, and how it was derived.

        ``sum``/``count`` are cumulative from the billing start minus what earlier invoices
        billed, so usage recorded after its period closed lands on the next invoice. ``max`` and
        ``unique_count`` are not additive: they read the period alone (late events are not
        billed once the period is closed).
        """
        metric_code = entry.metric_code
        if metric_code is None:  # usage prices pin their metric
            raise Conflict(f"Price {entry.price.code} rates no metric.")
        start = min(subscription.billing_start, period.start)
        cumulative = await aggregate_usage(
            session,
            project_id=subscription.project_id,
            customer_id=subscription.customer_id,
            metric_code=metric_code,
            start=start,
            end=period.end,
        )
        if cumulative.aggregation not in CUMULATIVE:
            alone = await aggregate_usage(
                session,
                project_id=subscription.project_id,
                customer_id=subscription.customer_id,
                metric_code=metric_code,
                start=period.start,
                end=period.end,
            )
            quantity = alone.value or Decimal(0)
            return quantity, {
                "aggregation": alone.aggregation.value,
                "range_start": period.start.isoformat(),
                "range_end": period.end.isoformat(),
                "aggregate": str(quantity),
            }
        total = cumulative.value or Decimal(0)
        billed = await repository.billed_quantity(
            session, subscription.project_id, subscription.id, entry.price.id
        )
        quantity = max(total - billed, Decimal(0))
        return quantity, {
            "aggregation": cumulative.aggregation.value,
            "range_start": start.isoformat(),
            "range_end": period.end.isoformat(),
            "aggregate": str(total),
            "previously_billed": str(billed),
        }

    async def _draft(
        self, uow: UnitOfWork, subscription: Subscription, period: Period
    ) -> tuple[Invoice, bool]:
        """Rate the period into a draft invoice: ``(invoice, created)`` (one per period)."""
        session = uow.session
        existing = await repository.find_invoice_by_period(
            session, subscription.project_id, subscription.id, period.start
        )
        if existing is not None:
            return existing, False
        plan = await self._plan_of(session, subscription)
        pricing = PricingService(price_book(session, clock=self._clock))
        rating_period = BillingPeriod(period.start, period.end)
        proration = _proration(period, subscription.anchor_day)
        lines: list[InvoiceLine] = []
        for position, entry in enumerate(await self._plan_entries(session, plan)):
            usage_details: dict[str, Any] | None = None
            if entry.metric_code is None:
                quantity: Decimal | None = None
                rated = await pricing.rate(
                    subscription.project_id,
                    entry.price.code,
                    Decimal(1),
                    rating_period,
                    proration=proration,
                )
            else:
                quantity, usage_details = await self._usage(session, subscription, entry, period)
                rated = await pricing.rate(
                    subscription.project_id,
                    entry.price.code,
                    AggregatedUsage(quantity, entry.metric_code, 1),
                    rating_period,
                    proration=proration,
                )
            lines.append(
                InvoiceLine(
                    id=new_uuid(),
                    position=position,
                    kind=LineKind.FIXED if entry.metric_code is None else LineKind.USAGE,
                    price_id=rated.price_id,
                    price_code=entry.price.code,
                    price_version_id=rated.price_version_id,
                    price_version=rated.price_version,
                    model=rated.model,
                    metric_code=rated.metric_code,
                    quantity=quantity,
                    amount=rated.total.minor_units,
                    rounding=rated.rounding,
                    details=_line_details(rated, proration, usage_details),
                )
            )
        amounts = invoice_amounts(line.amount for line in lines)
        now = self._clock.now()
        invoice = Invoice(
            id=new_uuid(),
            project_id=subscription.project_id,
            subscription_id=subscription.id,
            customer_id=subscription.customer_id,
            currency=plan.currency,
            status=InvoiceStatus.DRAFT,
            period_start=period.start,
            period_end=period.end,
            total=amounts.total,
            credits_applied=amounts.credits_applied,
            amount_due=amounts.amount_due,
            payment_intent_id=None,
            due_date=None,
            finalized_at=None,
            paid_at=None,
            voided_at=None,
            created_at=now,
            updated_at=now,
            lines=tuple(lines),
        )
        if not await repository.insert_invoice(session, invoice):
            found = await repository.find_invoice_by_period(
                session, subscription.project_id, subscription.id, period.start
            )
            if found is None:  # pragma: no cover - the conflicting row is visible to us
                raise Conflict("The period is being invoiced concurrently; retry.")
            return found, False
        await self._invoice_event(uow, billing_events.INVOICE_CREATED.name, invoice)
        return invoice, True

    # --- invoices -------------------------------------------------------------------------------

    async def get_invoice(self, project_id: UUID, invoice_id: UUID) -> Invoice:
        async with self._uow() as uow:
            invoice = await repository.get_invoice(uow.session, project_id, invoice_id)
        if invoice is None:
            raise NotFound("Invoice not found.")
        return invoice

    async def list_invoices(
        self, project_id: UUID, params: PageParams, *, subscription_id: UUID | None = None
    ) -> Page[Invoice]:
        async with self._uow() as uow:
            rows = await repository.list_invoices(
                uow.session,
                project_id,
                cursor=params.decoded_cursor(),
                limit=params.limit + 1,
                subscription_id=subscription_id,
            )
        return build_page(
            rows,
            params.limit,
            lambda invoice: invoice,
            lambda invoice: Cursor(created_at=invoice.created_at, id=invoice.id),
        )

    async def finalize_invoice(self, scope: ProjectScope, invoice_id: UUID) -> Invoice:
        """Finalize a draft by hand (the close finalizes its drafts itself)."""
        operator_only(scope)
        async with self._uow() as uow:
            invoice = await repository.get_invoice(
                uow.session, scope.project_id, invoice_id, lock=True
            )
            if invoice is None:
                raise NotFound("Invoice not found.")
            if invoice.status is not InvoiceStatus.DRAFT:
                raise Conflict(f"Only a draft invoice can be finalized (it is {invoice.status}).")
            subscription = await repository.get_subscription(
                uow.session, scope.project_id, invoice.subscription_id
            )
            if subscription is None:  # pragma: no cover - foreign key
                raise NotFound("Subscription not found.")
            finalized = await self._finalize(uow, invoice, subscription)
            await self._record(
                uow,
                scope,
                "invoice.finalized",
                AuditTarget("invoice", format_id(IdPrefix.INVOICE, invoice.id)),
                {"total": invoice.total, "amount_due": invoice.amount_due},
            )
        return finalized

    async def _finalize(
        self, uow: UnitOfWork, invoice: Invoice, subscription: Subscription
    ) -> Invoice:
        """R1 + the payment intent, in ``uow`` (the caller holds the invoice or its period)."""
        if not can_move(invoice.status, InvoiceStatus.FINALIZED):
            raise Conflict(f"Only a draft invoice can be finalized (it is {invoice.status}).")
        session = uow.session
        now = self._clock.now()
        currency = Currency.of(invoice.currency)
        if invoice.total > 0:
            await apply_recipes(
                uow,
                [
                    InvoiceFinalized(
                        invoice_id=invoice.id,
                        customer_id=CustomerId(invoice.customer_id),
                        sales_amount=Money(invoice.total, currency),
                        wallet_applied=Money.zero(currency),
                    )
                ],
                context=RecipeContext(
                    project_id=invoice.project_id, effective_at=now, correlation_id=invoice.id
                ),
            )
        due_date = self._payments.today() + timedelta(days=subscription.days_until_due)
        finalized = replace(
            invoice,
            status=InvoiceStatus.FINALIZED,
            due_date=due_date,
            finalized_at=now,
            updated_at=now,
        )
        if invoice.amount_due > 0:
            pending = await self._payments.open_invoice_payment(
                uow,
                project_id=invoice.project_id,
                customer_id=invoice.customer_id,
                invoice_id=invoice.id,
                amount=Money(invoice.amount_due, currency),
                method=subscription.payment_method,
                due_date=due_date,
                description=f"Invoice {format_id(IdPrefix.INVOICE, invoice.id)}",
            )
            finalized = replace(finalized, payment_intent_id=pending.intent.id)
            await repository.update_invoice(session, finalized)

            async def send() -> None:
                await self._payments.send_pending(pending)

            uow.after_commit.append(send)
            await self._invoice_event(uow, billing_events.INVOICE_FINALIZED.name, finalized)
            return finalized
        await repository.update_invoice(session, finalized)
        await self._invoice_event(uow, billing_events.INVOICE_FINALIZED.name, finalized)
        paid = replace(finalized, status=InvoiceStatus.PAID, paid_at=now)
        await repository.update_invoice(session, paid)
        await self._invoice_event(uow, billing_events.INVOICE_PAID.name, paid)
        return paid

    async def void_invoice(self, scope: ProjectScope, invoice_id: UUID) -> Invoice:
        """R11: void an invoice nothing was captured for (cancels its payment, reverses R1)."""
        operator_only(scope)
        async with self._uow() as uow:
            session = uow.session
            invoice = await repository.get_invoice(session, scope.project_id, invoice_id, lock=True)
            if invoice is None:
                raise NotFound("Invoice not found.")
            if not can_move(invoice.status, InvoiceStatus.VOID):
                raise Conflict(f"A {invoice.status} invoice cannot be voided.")
            if invoice.payment_intent_id is not None:
                await self._payments.cancel_invoice_payment(uow, invoice.payment_intent_id)
            now = self._clock.now()
            if invoice.status is not InvoiceStatus.DRAFT and invoice.total > 0:
                await apply_recipes(
                    uow,
                    [InvoiceVoided(invoice_id=invoice.id)],
                    context=RecipeContext(
                        project_id=invoice.project_id, effective_at=now, correlation_id=invoice.id
                    ),
                )
            voided = replace(invoice, status=InvoiceStatus.VOID, voided_at=now, updated_at=now)
            await repository.update_invoice(session, voided)
            await self._invoice_event(uow, billing_events.INVOICE_VOIDED.name, voided)
            await self._record(
                uow,
                scope,
                "invoice.voided",
                AuditTarget("invoice", format_id(IdPrefix.INVOICE, invoice.id)),
                {"previous_status": invoice.status.value},
            )
        return voided

    async def mark_paid(self, uow: UnitOfWork, intent_id: UUID) -> Invoice | None:
        """Mark the invoice of a captured intent paid, in ``uow`` (idempotent).

        Ignores intents of no invoice, invoices already paid or void (a capture after a void is
        unapplied cash, R2b) and intents that are not captured.
        """
        session = uow.session
        invoice = await repository.find_invoice_by_intent(session, intent_id, lock=True)
        if invoice is None or not can_move(invoice.status, InvoiceStatus.PAID):
            return None
        if not await is_captured(session, intent_id):
            return None
        now = self._clock.now()
        paid = replace(invoice, status=InvoiceStatus.PAID, paid_at=now, updated_at=now)
        await repository.update_invoice(session, paid)
        await self._invoice_event(uow, billing_events.INVOICE_PAID.name, paid)
        return paid
