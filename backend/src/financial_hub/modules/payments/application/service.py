"""Payment use cases and the external-call protocol (ADR-0012 s3, docs/DOMAIN.md s2).

Every provider call follows the same three steps, so a crash, timeout or duplicate delivery at
any point converges to exactly one recorded outcome:

1. **Transaction A** persists the intent (or refund) and an outbound attempt whose id is the
   *fencing token*, publishes the events, and commits. Nothing has been sent yet.
2. The provider is called **outside any transaction**, carrying ``external_reference`` = the
   hub's own id (``pi_`` / ``rf_``).
3. **Transaction B** records the outcome with a compare-and-set on ``(id, attempt_id, status)``:
   only the current token's holder may write, so a stale worker (whose token was replaced) or a
   late response (whose outcome the resolver already recorded) loses and records nothing.

A timeout, 5xx or retryable error leaves the attempt ``unknown`` (the public status stays
``processing``). A create is **never** re-sent blindly: :meth:`PaymentService.resolve_payment`
first looks the payment up by its external reference; only a *confirmed* absence opens a new
attempt (a new token) and re-sends. Refunds converge the same way through the payment's refund list.
"""

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any, Final
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from financial_hub.modules.audit.application import Actor, AuditContext, AuditRecorder, AuditTarget
from financial_hub.modules.customers.application import api as customers_api
from financial_hub.modules.events.application import EventDraft, EventPublisher
from financial_hub.modules.payments.application import events as payment_events
from financial_hub.modules.payments.application.ledger_postings import (
    LedgerHook,
    PaymentChange,
    post_payment_changes,
    with_reported_net,
)
from financial_hub.modules.payments.application.registry import (
    ContactLoader,
    CredentialsLoader,
    ProviderRegistry,
)
from financial_hub.modules.payments.domain import (
    FAKE_PROVIDER,
    TERMINAL_REFUND_STATES,
    Attempt,
    AttemptKind,
    AttemptStatus,
    CaptureStatus,
    CreatePaymentRequest,
    CreationStatus,
    DisputeStatus,
    IllegalTransition,
    PaymentIntent,
    PaymentMethod,
    PaymentProvider,
    PaymentState,
    PaymentStatus,
    ProviderCustomer,
    ProviderCustomerRequest,
    ProviderDuplicate,
    ProviderError,
    ProviderPayment,
    ProviderPaymentSnapshot,
    ProviderRefundSnapshot,
    ProviderRejected,
    ProviderUnavailable,
    ProviderUncertain,
    ProvisionStatus,
    Refund,
    RefundExceedsRefundable,
    RefundRequest,
    RefundState,
    StaleTransition,
    check_refund,
    move_capture,
    move_dispute,
    move_refund,
    move_settlement,
    public_status,
    refund_status,
    refundable_amount,
    refunded_amount,
    validate_amount,
    validate_description,
    validate_external_ref,
    validate_metadata,
)
from financial_hub.modules.payments.infrastructure import repository, webhook_repository
from financial_hub.modules.projects.application import api as projects_api
from financial_hub.platform.db import UnitOfWork
from financial_hub.platform.http.auth import ProjectScope
from financial_hub.shared.clock import Clock
from financial_hub.shared.errors import Conflict, FieldError, NotFound, ValidationFailed
from financial_hub.shared.ids import IdPrefix, InvalidIdError, format_id, new_uuid, parse_id
from financial_hub.shared.money import Currency, Money, UnknownCurrencyError
from financial_hub.shared.pagination import Cursor, Page, PageParams, build_page

logger = structlog.get_logger()

_EVENT_TYPES = payment_events  # importing the module registers the event types

# Boleto/PIX due dates are Brazilian calendar days (UTC-3, no daylight saving since 2019).
DUE_DATE_TZ: Final = timezone(timedelta(hours=-3))
# A ``create_requested`` attempt younger than this may still be in flight: the resolver waits.
DEFAULT_LEASE: Final = timedelta(minutes=5)
DEFAULT_CALL_TIMEOUT_SECONDS: Final = 30.0
BRL_ONLY_METHODS: Final = frozenset({PaymentMethod.PIX, PaymentMethod.BOLETO})
_UNRESOLVED: Final = (CreationStatus.CREATE_REQUESTED, CreationStatus.UNKNOWN)
_OPEN_REFUND: Final = (RefundState.REQUESTED, RefundState.UNKNOWN)


# --- views ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RefundView:
    id: str
    payment_id: str
    amount: int
    currency: str
    status: str
    origin: str
    reason: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PaymentView:
    """What an API caller sees. Amounts are integer minor units; ``status`` is derived."""

    id: str
    project_id: str
    customer_id: str
    amount: int
    currency: str
    method: str
    due_date: date
    external_ref: str | None
    description: str | None
    metadata: dict[str, Any]
    status: str
    provider: str
    provider_account_id: str | None
    provider_payment_id: str | None
    capture_status: str
    settlement_status: str
    dispute_status: str
    refund_status: str
    captured_amount: int
    refunded_amount: int
    refundable_amount: int
    failure_code: str | None
    refunds: tuple[RefundView, ...]
    created_at: datetime
    updated_at: datetime


def _refund_view(refund: Refund) -> RefundView:
    return RefundView(
        id=format_id(IdPrefix.REFUND, refund.id),
        payment_id=format_id(IdPrefix.PAYMENT_INTENT, refund.payment_intent_id),
        amount=refund.amount.minor_units,
        currency=refund.amount.currency.code,
        status=refund.status.value,
        origin=refund.origin,
        reason=refund.reason,
        created_at=refund.created_at,
        updated_at=refund.updated_at,
    )


def _pairs(refunds: Iterable[Refund]) -> list[tuple[RefundState, int]]:
    return [(refund.status, refund.amount.minor_units) for refund in refunds]


def derive_status(
    intent: PaymentIntent, payment: ProviderPayment | None, refunds: Sequence[Refund], today: date
) -> PaymentStatus:
    state = None if payment is None else payment.state
    captured = 0 if state is None else state.captured_amount
    return public_status(
        creation=intent.creation_status,
        state=state,
        refund=refund_status(captured, refunded_amount(_pairs(refunds))),
        due_date=intent.due_date,
        today=today,
    )


def payment_view(
    intent: PaymentIntent, payment: ProviderPayment | None, refunds: Sequence[Refund], today: date
) -> PaymentView:
    state = PaymentState() if payment is None else payment.state
    pairs = _pairs(refunds)
    return PaymentView(
        id=format_id(IdPrefix.PAYMENT_INTENT, intent.id),
        project_id=format_id(IdPrefix.PROJECT, intent.project_id),
        customer_id=format_id(IdPrefix.CUSTOMER, intent.customer_id),
        amount=intent.amount.minor_units,
        currency=intent.amount.currency.code,
        method=intent.method.value,
        due_date=intent.due_date,
        external_ref=intent.external_ref,
        description=intent.description,
        metadata=dict(intent.metadata),
        status=derive_status(intent, payment, refunds, today).value,
        provider=intent.provider,
        provider_account_id=(
            None
            if intent.provider_account_id is None
            else format_id(IdPrefix.PROVIDER_ACCOUNT, intent.provider_account_id)
        ),
        provider_payment_id=None if payment is None else payment.provider_payment_id,
        capture_status=state.capture_status.value,
        settlement_status=state.settlement_status.value,
        dispute_status=state.dispute_status.value,
        refund_status=refund_status(state.captured_amount, refunded_amount(pairs)).value,
        captured_amount=state.captured_amount,
        refunded_amount=refunded_amount(pairs),
        refundable_amount=refundable_amount(state.captured_amount, pairs),
        failure_code=intent.failure_code,
        refunds=tuple(_refund_view(refund) for refund in refunds),
        created_at=intent.created_at,
        updated_at=intent.updated_at,
    )


# --- errors and commands -------------------------------------------------------------------------


class PaymentExternalRefTaken(Conflict):
    """Another payment of the project already uses this ``external_ref`` (409)."""

    slug = "payment-external-ref-taken"
    title = "Payment external_ref already used"


class PaymentNotRefundable(Conflict):
    """The payment is not captured, is disputed, or has nothing left to refund (409)."""

    slug = "payment-not-refundable"
    title = "Payment cannot be refunded"


def _invalid(pointer: str, detail: str, code: str = "invalid") -> ValidationFailed:
    return ValidationFailed(
        "The request contains invalid fields.",
        errors=[FieldError(pointer=pointer, detail=detail, code=code)],
    )


@dataclass(frozen=True, slots=True)
class NewPayment:
    amount: int
    currency: str
    method: str
    due_date: date
    customer_id: str | None = None
    customer_external_ref: str | None = None
    external_ref: str | None = None
    description: str | None = None
    metadata: Mapping[str, Any] | None = None
    provider_account_id: str | None = None


@dataclass(frozen=True, slots=True)
class PendingPayment:
    """An intent recorded in transaction A whose provider call is still to be sent."""

    provider: PaymentProvider
    intent: PaymentIntent
    attempt_id: UUID
    request: CreatePaymentRequest


@dataclass(frozen=True, slots=True)
class _Outcome:
    """What one provider call produced: a result, or the error that says what to assume."""

    result: ProviderPaymentSnapshot | ProviderRefundSnapshot | ProviderCustomer | None
    error: ProviderError | None


def _request_hash(values: Mapping[str, Any]) -> bytes:
    canonical = json.dumps(values, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).digest()


def apply_snapshot(state: PaymentState, snapshot: ProviderPaymentSnapshot) -> PaymentState:
    """Move ``state`` towards a fetched snapshot, field by field; stale moves are ignored.

    An illegal move (a contradiction with what was recorded) leaves that field as it is and is
    logged; reconciliation (PRO-43) surfaces it.
    """
    moves: tuple[Callable[[PaymentState], PaymentState], ...] = (
        lambda s: move_capture(
            s,
            snapshot.capture_status,
            amount=snapshot.amount.minor_units,
            net_amount=snapshot.net_amount,
        ),
        lambda s: move_settlement(s, snapshot.settlement_status),
        lambda s: move_dispute(s, snapshot.dispute_status),
    )
    for move in moves:
        try:
            state = move(state)
        except StaleTransition:
            continue
        except IllegalTransition as error:
            logger.warning(
                "payments.illegal_transition",
                field=error.field,
                current=error.current,
                target=error.target,
            )
    return state


# --- the service ---------------------------------------------------------------------------------


class PaymentService:
    """Each method opens its own short transactions (the protocol needs two around each call)."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Clock,
        registry: ProviderRegistry,
        credentials: CredentialsLoader | None = None,
        contacts: ContactLoader | None = None,
        audit: AuditContext | None = None,
        publisher: EventPublisher | None = None,
        ledger_hook: LedgerHook = post_payment_changes,
        lease: timedelta = DEFAULT_LEASE,
        call_timeout_seconds: float = DEFAULT_CALL_TIMEOUT_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._registry = registry
        self._credentials = credentials
        # Without it the hub never provisions provider customers (the pre-W4.4 behaviour).
        self._contacts = contacts
        self._audit = audit or AuditContext.system()
        self._publisher = publisher or EventPublisher(clock=clock, notifier=None)
        # Called in every transaction that records a payment or refund transition (PRO-42).
        self._ledger_hook = ledger_hook
        self._lease = lease
        self._call_timeout = call_timeout_seconds

    def _uow(self) -> UnitOfWork:
        return UnitOfWork(self._session_factory)

    def today(self) -> date:
        return self._clock.now().astimezone(DUE_DATE_TZ).date()

    # --- helpers --------------------------------------------------------------------------------

    async def _publish(
        self, uow: UnitOfWork, project_id: UUID, event_type: str, subject: str, **data: Any
    ) -> None:
        await self._publisher.publish(
            uow, EventDraft(type=event_type, data=data, project_id=project_id, subject=subject)
        )

    async def _status_changed(
        self, uow: UnitOfWork, intent: PaymentIntent, before: PaymentStatus, after: PaymentStatus
    ) -> None:
        if before is after:
            return
        payment_id = format_id(IdPrefix.PAYMENT_INTENT, intent.id)
        await self._publish(
            uow,
            intent.project_id,
            payment_events.PAYMENT_STATUS_CHANGED.name,
            payment_id,
            payment_id=payment_id,
            previous_status=before.value,
            status=after.value,
        )

    async def _provider(
        self, session: AsyncSession, project_id: UUID, provider: str, account_id: UUID | None
    ) -> PaymentProvider:
        credentials = None
        if provider != FAKE_PROVIDER and account_id is not None and self._credentials is not None:
            credentials = await self._credentials(session, project_id, account_id)
        return self._registry.resolve(provider, account_id, credentials)

    async def _choose_account(
        self, session: AsyncSession, project_id: UUID, account_id: str | None
    ) -> tuple[str, UUID | None]:
        """The account named in the request, else the only active one, else the fake provider."""
        if account_id is not None:
            try:
                parsed = parse_id(account_id, IdPrefix.PROVIDER_ACCOUNT)
            except InvalidIdError as error:
                raise _invalid("/provider_account_id", str(error)) from None
            ref = await projects_api.provider_account_ref(session, project_id, parsed)
            if ref is None or not ref.active:
                raise _invalid(
                    "/provider_account_id", "no active provider account with this id", "not_found"
                )
            return ref.provider, ref.id
        accounts = await projects_api.active_provider_accounts(session, project_id)
        if len(accounts) == 1:
            return accounts[0].provider, accounts[0].id
        if len(accounts) > 1:
            raise _invalid(
                "/provider_account_id",
                "the project has several active provider accounts: name one",
                "required",
            )
        if self._registry.fake_enabled:
            return FAKE_PROVIDER, None
        raise _invalid(
            "/provider_account_id", "the project has no active provider account", "required"
        )

    async def _resolve_provider(
        self, session: AsyncSession, project_id: UUID, provider: str, account_id: UUID | None
    ) -> PaymentProvider:
        try:
            return await self._provider(session, project_id, provider, account_id)
        except ProviderUnavailable as error:
            raise _invalid(
                "/provider_account_id",
                "this provider account cannot take payments yet",
                error.code,
            ) from None

    async def _call(self, call: Awaitable[Any]) -> _Outcome:
        """Run one provider call with a deadline; classify anything unexpected as uncertain."""
        try:
            async with asyncio.timeout(self._call_timeout):
                return _Outcome(await call, None)
        except ProviderError as error:
            return _Outcome(None, error)
        except TimeoutError:
            return _Outcome(None, ProviderUncertain("timeout"))
        except Exception as error:  # an adapter bug must not leave the outcome unrecorded
            logger.warning("payments.provider_call_failed", error=type(error).__name__)
            return _Outcome(None, ProviderUncertain("unexpected_error"))

    # --- create ---------------------------------------------------------------------------------

    def _validated(
        self, new: NewPayment
    ) -> tuple[Money, PaymentMethod, str | None, str | None, dict[str, Any]]:
        if type(new.amount) is not int:
            raise _invalid("/amount", "must be an integer amount in minor units")
        try:
            currency = Currency.of(new.currency)
        except UnknownCurrencyError, TypeError:
            raise _invalid("/currency", "must be an ISO 4217 currency code") from None
        try:
            amount = validate_amount(Money(new.amount, currency))
        except ValueError as error:
            raise _invalid("/amount", str(error)) from None
        try:
            method = PaymentMethod(new.method)
        except ValueError:
            raise _invalid("/method", "must be one of: pix, boleto, card") from None
        if method in BRL_ONLY_METHODS and currency.code != "BRL":
            raise _invalid("/currency", f"{method.value} payments are in BRL only")
        if new.due_date < self.today():
            raise _invalid("/due_date", "must be today or later")
        external_ref = None
        if new.external_ref is not None:
            try:
                external_ref = validate_external_ref(new.external_ref)
            except ValueError as error:
                raise _invalid("/external_ref", str(error)) from None
        try:
            description = validate_description(new.description)
        except ValueError as error:
            raise _invalid("/description", str(error)) from None
        try:
            metadata = validate_metadata(new.metadata or {})
        except ValueError as error:
            raise _invalid("/metadata", str(error)) from None
        return amount, method, external_ref, description, metadata

    async def create(self, scope: ProjectScope, new: NewPayment) -> PaymentView:
        """Transaction A, the provider call, then transaction B (see the module docstring)."""
        if new.customer_id is None and new.customer_external_ref is None:
            raise _invalid("/customer", "give customer.id or customer.external_ref", "required")
        amount, method, external_ref, description, metadata = self._validated(new)
        project_id = scope.project_id
        async with self._uow() as uow:
            customer = await customers_api.resolve_customer_identity(
                uow.session,
                project_id=project_id,
                customer_id=new.customer_id,
                external_ref=new.customer_external_ref,
            )
            if customer is None:
                raise _invalid("/customer", "unknown or anonymized customer", "not_found")
            pending = await self._open(
                uow,
                project_id=project_id,
                customer=customer,
                amount=amount,
                method=method,
                due_date=new.due_date,
                external_ref=external_ref,
                description=description,
                metadata=metadata,
                provider_account_id=new.provider_account_id,
            )
        await self.send_pending(pending)
        return await self._load_view(project_id, pending.intent.id)

    async def _open(
        self,
        uow: UnitOfWork,
        *,
        project_id: UUID,
        customer: UUID,
        amount: Money,
        method: PaymentMethod,
        due_date: date,
        external_ref: str | None,
        description: str | None,
        metadata: Mapping[str, Any],
        provider_account_id: str | None,
        invoice_id: UUID | None = None,
    ) -> PendingPayment:
        """Transaction A in the caller's unit of work: the intent, its attempt and the event."""
        session = uow.session
        intent_id, attempt_id = new_uuid(), new_uuid()
        provider_name, account_id = await self._choose_account(
            session, project_id, provider_account_id
        )
        provider = await self._resolve_provider(session, project_id, provider_name, account_id)
        provider_customer = (
            None
            if account_id is None
            else await customers_api.get_provider_customer(
                session,
                project_id=project_id,
                identity_id=customer,
                provider_account_id=account_id,
            )
        )
        now = self._clock.now()
        intent = PaymentIntent(
            id=intent_id,
            project_id=project_id,
            customer_id=customer,
            amount=amount,
            method=method,
            due_date=due_date,
            external_ref=external_ref,
            description=description,
            metadata=metadata,
            provider=provider_name,
            provider_account_id=account_id,
            creation_status=CreationStatus.CREATE_REQUESTED,
            attempt_id=attempt_id,
            failure_code=None,
            created_at=now,
            updated_at=now,
            invoice_id=invoice_id,
        )
        if not await repository.insert_intent(session, intent):
            raise PaymentExternalRefTaken("Another payment of this project uses this external_ref.")
        request = self._create_request(intent, provider_customer)
        await self._insert_attempt(session, intent, attempt_id, request, now)
        payment_id = format_id(IdPrefix.PAYMENT_INTENT, intent_id)
        await self._publish(
            uow,
            project_id,
            payment_events.PAYMENT_CREATED.name,
            payment_id,
            payment_id=payment_id,
            customer_id=format_id(IdPrefix.CUSTOMER, customer),
            status=derive_status(intent, None, (), self.today()).value,
        )
        return PendingPayment(provider, intent, attempt_id, request)

    async def send_pending(self, pending: PendingPayment) -> None:
        """The provider call and transaction B of an intent opened by ``_open`` (after commit)."""
        ready = await self._with_customer(
            pending.provider, pending.intent, pending.attempt_id, pending.request
        )
        if ready is not None:
            await self._send_create(pending.provider, pending.intent, pending.attempt_id, ready)

    # --- invoice payments (W5.3) ---------------------------------------------------------------

    async def open_invoice_payment(
        self,
        uow: UnitOfWork,
        *,
        project_id: UUID,
        customer_id: UUID,
        invoice_id: UUID,
        amount: Money,
        method: str,
        due_date: date,
        description: str | None = None,
    ) -> PendingPayment:
        """Open the one intent collecting ``invoice_id`` in billing's finalize transaction.

        The caller sends it after its commit (:meth:`send_pending`, e.g. from ``after_commit``); a
        crash in between leaves a ``create_requested`` intent the resolver converges. The intent
        is linked by ``invoice_id`` (never by metadata a product could forge), so the ledger hook
        books no R1 for it: billing booked R1 at finalize.
        """
        try:
            checked = validate_amount(amount)
        except ValueError as error:
            raise _invalid("/amount", str(error)) from None
        try:
            payment_method = PaymentMethod(method)
        except ValueError:
            raise _invalid("/method", "must be one of: pix, boleto, card") from None
        if payment_method in BRL_ONLY_METHODS and checked.currency.code != "BRL":
            raise _invalid("/currency", f"{payment_method.value} payments are in BRL only")
        if due_date < self.today():
            raise _invalid("/due_date", "must be today or later")
        try:
            text = validate_description(description)
        except ValueError as error:
            raise _invalid("/description", str(error)) from None
        return await self._open(
            uow,
            project_id=project_id,
            customer=customer_id,
            amount=checked,
            method=payment_method,
            due_date=due_date,
            external_ref=None,
            description=text,
            metadata={"invoice_id": format_id(IdPrefix.INVOICE, invoice_id)},
            provider_account_id=None,
            invoice_id=invoice_id,
        )

    async def cancel_invoice_payment(self, uow: UnitOfWork, intent_id: UUID) -> PaymentIntent:
        """Cancel the intent of an invoice being voided, in billing's void transaction (R11).

        Refuses (``Conflict``) when money was captured or the creation outcome is not known yet.
        A pending provider payment is marked ``canceled`` hub-side (so a capture that still
        arrives is a late capture, booked as unapplied cash R2b) and canceled at the provider
        after the commit, best effort.
        """
        session = uow.session
        intent = await repository.find_intent(session, intent_id)
        if intent is None:
            raise NotFound("Payment not found.")
        payment = await repository.get_provider_payment(session, intent.id, for_update=True)
        if payment is None:
            if intent.creation_status in _UNRESOLVED:
                raise Conflict("The invoice's payment is still being created; retry later.")
            return intent  # rejected: nothing exists at the provider
        if payment.state.capture_status is CaptureStatus.CAPTURED:
            raise Conflict("The invoice's payment was captured; refund it instead of voiding.")
        if payment.state.capture_status is not CaptureStatus.PENDING:
            return intent  # already failed or canceled
        now = self._clock.now()
        today = self.today()
        refunds = list(await repository.refunds_of(session, [intent.id]))
        before = derive_status(intent, payment, refunds, today)
        state = move_capture(payment.state, CaptureStatus.CANCELED)
        await repository.update_provider_payment(
            session, payment.id, state=state, raw_status=payment.raw_status, now=now
        )
        canceled = replace(payment, state=state, updated_at=now)
        await self._ledger_hook(
            uow, PaymentChange(intent, canceled, payment.state, now, refunds, refunds)
        )
        after = derive_status(intent, canceled, refunds, today)
        await self._status_changed(uow, intent, before, after)
        try:
            provider = await self._provider(
                session, intent.project_id, intent.provider, intent.provider_account_id
            )
        except ProviderUnavailable:
            logger.warning("payments.invoice_cancel_skipped", payment_intent_id=str(intent_id))
            return intent
        provider_payment_id = payment.provider_payment_id

        async def cancel_at_provider() -> None:
            outcome = await self._call(provider.cancel(provider_payment_id))
            if outcome.error is not None:
                logger.warning(
                    "payments.invoice_cancel_failed",
                    payment_intent_id=str(intent_id),
                    code=outcome.error.code,
                )

        uow.after_commit.append(cancel_at_provider)
        return intent

    @staticmethod
    def _create_request(
        intent: PaymentIntent, provider_customer_id: str | None
    ) -> CreatePaymentRequest:
        return CreatePaymentRequest(
            external_reference=format_id(IdPrefix.PAYMENT_INTENT, intent.id),
            amount=intent.amount,
            method=intent.method,
            due_date=intent.due_date,
            customer_reference=format_id(IdPrefix.CUSTOMER, intent.customer_id),
            provider_customer_id=provider_customer_id,
            description=intent.description,
        )

    @staticmethod
    async def _insert_attempt(
        session: AsyncSession,
        intent: PaymentIntent,
        attempt_id: UUID,
        request: CreatePaymentRequest,
        now: datetime,
    ) -> None:
        digest = _request_hash(
            {
                "external_reference": request.external_reference,
                "amount": request.amount.minor_units,
                "currency": request.amount.currency.code,
                "method": request.method.value,
                "due_date": request.due_date.isoformat(),
                "customer_reference": request.customer_reference,
            }
        )
        await repository.insert_attempt(
            session,
            Attempt(
                id=attempt_id,
                project_id=intent.project_id,
                kind=AttemptKind.CREATE_PAYMENT,
                payment_intent_id=intent.id,
                refund_id=None,
                status=AttemptStatus.REQUESTED,
                request_hash=digest,
                created_at=now,
            ),
        )

    # --- customer provisioning (W4.4) ---------------------------------------------------------

    async def _with_customer(
        self,
        provider: PaymentProvider,
        intent: PaymentIntent,
        attempt_id: UUID,
        request: CreatePaymentRequest,
    ) -> CreatePaymentRequest | None:
        """The request carrying the provider customer, provisioned first when there is none.

        ``None`` when provisioning did not succeed: the payment's creation outcome is then already
        recorded (``rejected``, or ``unknown`` for the resolver, which provisions again before it
        re-sends). The charge is never sent without a customer the provider confirmed.
        """
        account_id = intent.provider_account_id
        if request.provider_customer_id is not None or account_id is None or self._contacts is None:
            return request
        provisioned = await self._provision_customer(provider, intent, account_id)
        if isinstance(provisioned, str):
            return replace(request, provider_customer_id=provisioned)
        rejected, code = provisioned
        if rejected:
            await self._record_rejected(intent, attempt_id, code)
        else:
            await self._record_unknown(intent, attempt_id, code)
        return None

    async def _provision_customer(
        self, provider: PaymentProvider, intent: PaymentIntent, account_id: UUID
    ) -> str | tuple[bool, str]:
        """Create (or find) the identity's provider customer under the external-call protocol.

        Transaction A takes the provisioning token (a lease while the call may be in flight) and
        loads the contact; the call carries ``externalReference`` = the hub's ``cus_`` id, and the
        adapter looks that reference up before creating, so a retry after a lost answer finds the
        customer instead of doubling it; transaction B links the mapping under the token. Returns
        the provider customer id, or ``(rejected, code)``. The decrypted document lives only in
        this frame and the call; it is never logged.
        """
        loader = self._contacts
        if loader is None:  # pragma: no cover - _with_customer checks it first
            return False, "customer_contacts_unavailable"
        token = new_uuid()
        project_id, identity_id = intent.project_id, intent.customer_id
        async with self._uow() as uow:
            session = uow.session
            now = self._clock.now()
            claimed = await webhook_repository.claim_provision(
                session,
                identity_id=identity_id,
                provider_account_id=account_id,
                project_id=project_id,
                attempt_id=token,
                lease_until=now + self._lease,
                now=now,
            )
            existing = await customers_api.get_provider_customer(
                session,
                project_id=project_id,
                identity_id=identity_id,
                provider_account_id=account_id,
            )
            if existing is not None:  # a concurrent provisioning (or a manual link) came first
                if claimed:
                    await webhook_repository.finish_provision(
                        session,
                        identity_id=identity_id,
                        provider_account_id=account_id,
                        attempt_id=token,
                        status=ProvisionStatus.CREATED,
                        failure_code=None,
                        now=now,
                    )
                return existing
            if not claimed:
                return False, "customer_provisioning_in_flight"
            contact = await loader(session, project_id, identity_id)
        if contact is None:
            await self._finish_provision(
                intent, account_id, token, ProvisionStatus.REJECTED, "customer_unavailable"
            )
            return True, "customer_unavailable"
        request = ProviderCustomerRequest(
            external_reference=format_id(IdPrefix.CUSTOMER, identity_id),
            name=contact.name,
            email=contact.email,
            phone=contact.phone,
        )
        outcome = await self._call(provider.create_customer(request, cpf_cnpj=contact.document))
        del contact
        if isinstance(outcome.result, ProviderCustomer):
            return await self._record_provisioned(
                intent, account_id, token, outcome.result.provider_customer_id
            )
        code = "unknown" if outcome.error is None else outcome.error.code
        rejected = isinstance(outcome.error, ProviderRejected)
        status = ProvisionStatus.REJECTED if rejected else ProvisionStatus.UNKNOWN
        await self._finish_provision(intent, account_id, token, status, code)
        logger.info(
            "payments.customer_provisioning_failed",
            customer_id=format_id(IdPrefix.CUSTOMER, identity_id),
            provider_account_id=format_id(IdPrefix.PROVIDER_ACCOUNT, account_id),
            rejected=rejected,
            code=code[:100],
        )
        return rejected, f"customer_{code}"[:100]

    async def _record_provisioned(
        self, intent: PaymentIntent, account_id: UUID, token: UUID, provider_customer_id: str
    ) -> str | tuple[bool, str]:
        async with self._uow() as uow:
            session = uow.session
            try:
                await customers_api.link_provider_customer(
                    session,
                    self._clock,
                    project_id=intent.project_id,
                    identity_id=intent.customer_id,
                    provider_account_id=account_id,
                    provider_customer_id=provider_customer_id,
                )
            except Conflict:
                # The identity already maps to another customer on this account (linked by hand,
                # or the provider answered with a different one): never guess which is right.
                logger.warning(
                    "payments.provider_customer_conflict",
                    customer_id=format_id(IdPrefix.CUSTOMER, intent.customer_id),
                    provider_account_id=format_id(IdPrefix.PROVIDER_ACCOUNT, account_id),
                )
                linked = None
            else:
                linked = provider_customer_id
            await webhook_repository.finish_provision(
                session,
                identity_id=intent.customer_id,
                provider_account_id=account_id,
                attempt_id=token,
                status=ProvisionStatus.CREATED if linked else ProvisionStatus.UNKNOWN,
                failure_code=None if linked else "mapping_conflict",
                now=self._clock.now(),
            )
        if linked is None:
            return False, "customer_mapping_conflict"
        logger.info(
            "payments.customer_provisioned",
            customer_id=format_id(IdPrefix.CUSTOMER, intent.customer_id),
            provider_account_id=format_id(IdPrefix.PROVIDER_ACCOUNT, account_id),
        )
        return linked

    async def _finish_provision(
        self,
        intent: PaymentIntent,
        account_id: UUID,
        token: UUID,
        status: ProvisionStatus,
        code: str,
    ) -> None:
        async with self._uow() as uow:
            await webhook_repository.finish_provision(
                uow.session,
                identity_id=intent.customer_id,
                provider_account_id=account_id,
                attempt_id=token,
                status=status,
                failure_code=code,
                now=self._clock.now(),
            )

    async def _send_create(
        self,
        provider: PaymentProvider,
        intent: PaymentIntent,
        attempt_id: UUID,
        request: CreatePaymentRequest,
    ) -> None:
        outcome = await self._call(provider.create_payment(request))
        if isinstance(outcome.result, ProviderPaymentSnapshot):
            await self._record_created(intent, attempt_id, outcome.result)
        elif isinstance(outcome.error, ProviderDuplicate):
            # An earlier attempt did create it: converge on what the provider holds.
            await self._lookup_and_record(provider, intent, attempt_id)
        elif isinstance(outcome.error, ProviderRejected):
            await self._record_rejected(intent, attempt_id, outcome.error.code)
        else:
            code = "unknown" if outcome.error is None else outcome.error.code
            await self._record_unknown(intent, attempt_id, code)

    async def _lookup_and_record(
        self, provider: PaymentProvider, intent: PaymentIntent, attempt_id: UUID
    ) -> bool | None:
        """Look the payment up; record it if found. ``None``: confirmed absent; error: unknown."""
        reference = format_id(IdPrefix.PAYMENT_INTENT, intent.id)
        outcome = await self._call(provider.lookup_by_external_reference(reference))
        if outcome.error is not None:
            await self._record_unknown(intent, attempt_id, outcome.error.code)
            return False
        if isinstance(outcome.result, ProviderPaymentSnapshot):
            return await self._record_created(intent, attempt_id, outcome.result)
        return None

    async def _record_created(
        self, intent: PaymentIntent, attempt_id: UUID, snapshot: ProviderPaymentSnapshot
    ) -> bool:
        """Transaction B for a created payment; ``False`` when another writer won the race."""
        async with self._uow() as uow:
            session = uow.session
            now = self._clock.now()
            won = await repository.record_creation(
                session,
                intent_id=intent.id,
                attempt_id=attempt_id,
                expected=_UNRESOLVED,
                status=CreationStatus.CREATED,
                failure_code=None,
                now=now,
            )
            if not won:
                await repository.finish_attempt(
                    session, attempt_id, status=AttemptStatus.SUPERSEDED, outcome={}, now=now
                )
                return False
            state = apply_snapshot(PaymentState(), snapshot)
            payment = ProviderPayment(
                id=new_uuid(),
                payment_intent_id=intent.id,
                project_id=intent.project_id,
                provider=intent.provider,
                provider_account_id=intent.provider_account_id,
                provider_payment_id=snapshot.provider_payment_id,
                raw_status=snapshot.raw_status[:100],
                state=state,
                created_at=now,
                updated_at=now,
            )
            await repository.insert_provider_payment(session, payment)
            await repository.finish_attempt(
                session,
                attempt_id,
                status=AttemptStatus.SUCCEEDED,
                outcome={"provider_payment_id": snapshot.provider_payment_id},
                now=now,
            )
            today = self.today()
            before = derive_status(intent, None, (), today)
            created = _with_creation(intent, CreationStatus.CREATED, None)
            await self._status_changed(
                uow, intent, before, derive_status(created, payment, (), today)
            )
            await self._ledger_hook(uow, PaymentChange(created, payment, PaymentState(), now))
        return True

    async def _record_rejected(self, intent: PaymentIntent, attempt_id: UUID, code: str) -> None:
        async with self._uow() as uow:
            session = uow.session
            now = self._clock.now()
            won = await repository.record_creation(
                session,
                intent_id=intent.id,
                attempt_id=attempt_id,
                expected=_UNRESOLVED,
                status=CreationStatus.REJECTED,
                failure_code=code[:100],
                now=now,
            )
            status = AttemptStatus.REJECTED if won else AttemptStatus.SUPERSEDED
            await repository.finish_attempt(
                session, attempt_id, status=status, outcome={"code": code[:100]}, now=now
            )
            if won:
                today = self.today()
                rejected = _with_creation(intent, CreationStatus.REJECTED, code[:100])
                await self._status_changed(
                    uow,
                    intent,
                    derive_status(intent, None, (), today),
                    derive_status(rejected, None, (), today),
                )

    async def _record_unknown(self, intent: PaymentIntent, attempt_id: UUID, code: str) -> None:
        async with self._uow() as uow:
            session = uow.session
            now = self._clock.now()
            won = await repository.record_creation(
                session,
                intent_id=intent.id,
                attempt_id=attempt_id,
                expected=(CreationStatus.CREATE_REQUESTED,),
                status=CreationStatus.UNKNOWN,
                failure_code=None,
                now=now,
            )
            if won:
                await repository.mark_attempt_unknown(
                    session, attempt_id, outcome={"code": code[:100]}
                )
            else:
                # Already unknown under this token (a repeated failure) keeps the attempt open;
                # anything else means another writer resolved it.
                current = await repository.find_intent(session, intent.id)
                if (
                    current is None
                    or current.attempt_id != attempt_id
                    or (current.creation_status is not CreationStatus.UNKNOWN)
                ):
                    await repository.finish_attempt(
                        session, attempt_id, status=AttemptStatus.SUPERSEDED, outcome={}, now=now
                    )
        logger.info(
            "payments.create_outcome_unknown",
            payment_id=format_id(IdPrefix.PAYMENT_INTENT, intent.id),
            code=code[:100],
        )

    # --- resolver -------------------------------------------------------------------------------

    async def resolve_payment(self, intent_id: UUID) -> CreationStatus | None:
        """Converge one intent whose creation outcome is unknown (the worker loop calls this).

        Looks the payment up by its external reference: found -> recorded under the current
        token; *confirmed* absent -> a new attempt (new token) re-sends the create. A
        ``create_requested`` intent younger than the lease is left alone (its call may be in
        flight). Returns the creation status afterwards (``None`` for an unknown id).
        """
        async with self._uow() as uow:
            intent = await repository.find_intent(uow.session, intent_id)
            if intent is None or intent.creation_status not in _UNRESOLVED:
                return None if intent is None else intent.creation_status
            fresh = intent.updated_at > self._clock.now() - self._lease
            if intent.creation_status is CreationStatus.CREATE_REQUESTED and fresh:
                return intent.creation_status
            try:
                provider = await self._provider(
                    uow.session, intent.project_id, intent.provider, intent.provider_account_id
                )
            except ProviderUnavailable as error:
                logger.warning("payments.resolver_provider_unavailable", code=error.code)
                return intent.creation_status
        found = await self._lookup_and_record(provider, intent, intent.attempt_id)
        if found is None:
            await self._recreate(provider, intent)
        return await self._creation_status(intent_id)

    async def _recreate(self, provider: PaymentProvider, intent: PaymentIntent) -> None:
        """The lookup confirmed the payment does not exist: replace the token and send again."""
        new_attempt = new_uuid()
        async with self._uow() as uow:
            session = uow.session
            now = self._clock.now()
            replaced = await repository.replace_creation_attempt(
                session,
                intent_id=intent.id,
                old_attempt_id=intent.attempt_id,
                new_attempt_id=new_attempt,
                now=now,
            )
            if not replaced:
                return
            await repository.finish_attempt(
                session,
                intent.attempt_id,
                status=AttemptStatus.ABSENT,
                outcome={"lookup": "absent"},
                now=now,
            )
            provider_customer = (
                None
                if intent.provider_account_id is None
                else await customers_api.get_provider_customer(
                    session,
                    project_id=intent.project_id,
                    identity_id=intent.customer_id,
                    provider_account_id=intent.provider_account_id,
                )
            )
            request = self._create_request(intent, provider_customer)
            await self._insert_attempt(session, intent, new_attempt, request, now)
        current = _with_creation(intent, CreationStatus.CREATE_REQUESTED, None, new_attempt)
        ready = await self._with_customer(provider, current, new_attempt, request)
        if ready is not None:
            await self._send_create(provider, current, new_attempt, ready)

    async def _creation_status(self, intent_id: UUID) -> CreationStatus | None:
        async with self._uow() as uow:
            intent = await repository.find_intent(uow.session, intent_id)
        return None if intent is None else intent.creation_status

    async def resolve_pending(self, *, limit: int = 100) -> int:
        """Resolve unknown creations and refunds; returns how many were looked at."""
        async with self._uow() as uow:
            cutoff = self._clock.now() - self._lease
            intents = await repository.unresolved_intents(
                uow.session, requested_before=cutoff, limit=limit
            )
            pending_refunds = await repository.unresolved_refunds(
                uow.session, requested_before=cutoff, limit=limit
            )
        for intent in intents:
            await self.resolve_payment(intent.id)
        for refund in pending_refunds:
            await self.resolve_refund(refund.id)
        return len(intents) + len(pending_refunds)

    # --- reads ----------------------------------------------------------------------------------

    async def _load_view(self, project_id: UUID, intent_id: UUID) -> PaymentView:
        async with self._uow() as uow:
            intent = await repository.get_intent(uow.session, project_id, intent_id)
            if intent is None:
                raise NotFound("Payment not found.")
            payment = await repository.get_provider_payment(uow.session, intent.id)
            refunds = await repository.refunds_of(uow.session, [intent.id])
        return payment_view(intent, payment, refunds, self.today())

    async def get(self, project_id: UUID, payment_id: str) -> PaymentView:
        return await self._load_view(project_id, _parse_payment_id(payment_id))

    async def list_payments(self, project_id: UUID, params: PageParams) -> Page[PaymentView]:
        async with self._uow() as uow:
            intents = await repository.list_intents(
                uow.session, project_id, cursor=params.decoded_cursor(), limit=params.limit + 1
            )
            ids = [intent.id for intent in intents]
            payments = await repository.provider_payments_for(uow.session, ids)
            refunds = await repository.refunds_of(uow.session, ids)
        by_intent: dict[UUID, list[Refund]] = {}
        for refund in refunds:
            by_intent.setdefault(refund.payment_intent_id, []).append(refund)
        today = self.today()
        return build_page(
            intents,
            params.limit,
            lambda intent: payment_view(
                intent, payments.get(intent.id), by_intent.get(intent.id, []), today
            ),
            lambda intent: Cursor(created_at=intent.created_at, id=intent.id),
        )

    # --- sync -----------------------------------------------------------------------------------

    async def refresh(self, intent_id: UUID) -> PaymentStatus | None:
        """Fetch the provider payment and apply it through the transitions (ADR-0012 s2).

        Stale moves are ignored; hub refunds follow the provider's refund list; refunds made at
        the provider (no hub reference) are recorded as ``origin = provider``. Returns the public
        status afterwards (``None`` when there is no provider payment yet).
        """
        async with self._uow() as uow:
            intent = await repository.find_intent(uow.session, intent_id)
            payment = (
                None
                if intent is None
                else await repository.get_provider_payment(uow.session, intent.id)
            )
            if intent is None or payment is None:
                return None
            provider = await self._provider(
                uow.session, intent.project_id, intent.provider, intent.provider_account_id
            )
        outcome = await self._call(provider.get_payment(payment.provider_payment_id))
        if not isinstance(outcome.result, ProviderPaymentSnapshot):
            return None
        return await self._apply(intent, outcome.result)

    async def _apply(
        self, intent: PaymentIntent, snapshot: ProviderPaymentSnapshot
    ) -> PaymentStatus:
        async with self._uow() as uow:
            session = uow.session
            now = self._clock.now()
            today = self.today()
            payment = await repository.get_provider_payment(session, intent.id, for_update=True)
            if payment is None:  # pragma: no cover - refresh only runs after creation
                raise NotFound("Payment not found.")
            refunds = list(await repository.refunds_of(session, [intent.id]))
            before = derive_status(intent, payment, refunds, today)
            state = apply_snapshot(payment.state, snapshot)
            state = await with_reported_net(uow, intent, payment, state, snapshot.net_amount)
            if state != payment.state or snapshot.raw_status[:100] != payment.raw_status:
                await repository.update_provider_payment(
                    session, payment.id, state=state, raw_status=snapshot.raw_status[:100], now=now
                )
            previous = payment.state
            payment = ProviderPayment(
                id=payment.id,
                payment_intent_id=payment.payment_intent_id,
                project_id=payment.project_id,
                provider=payment.provider,
                provider_account_id=payment.provider_account_id,
                provider_payment_id=payment.provider_payment_id,
                raw_status=snapshot.raw_status[:100],
                state=state,
                created_at=payment.created_at,
                updated_at=now,
            )
            await self._sync_refunds(session, intent, payment, refunds, snapshot.refunds, now)
            synced = list(await repository.refunds_of(session, [intent.id]))
            await self._ledger_hook(
                uow, PaymentChange(intent, payment, previous, now, refunds, synced)
            )
            after = derive_status(intent, payment, synced, today)
            await self._status_changed(uow, intent, before, after)
        return after

    @staticmethod
    async def _sync_refunds(
        session: AsyncSession,
        intent: PaymentIntent,
        payment: ProviderPayment,
        known: Sequence[Refund],
        reported: Sequence[ProviderRefundSnapshot],
        now: datetime,
    ) -> None:
        by_reference = {format_id(IdPrefix.REFUND, refund.id): refund for refund in known}
        by_provider_id = {r.provider_refund_id: r for r in known if r.provider_refund_id}
        for snapshot in reported:
            ours = by_provider_id.get(snapshot.provider_refund_id) or (
                None
                if snapshot.external_reference is None
                else by_reference.get(snapshot.external_reference)
            )
            if ours is None:
                if snapshot.status is RefundState.DONE:
                    await repository.insert_refund(
                        session,
                        Refund(
                            id=new_uuid(),
                            project_id=intent.project_id,
                            payment_intent_id=intent.id,
                            provider_payment_id=payment.id,
                            amount=snapshot.amount,
                            status=RefundState.DONE,
                            reason=None,
                            provider_refund_id=snapshot.provider_refund_id,
                            attempt_id=None,
                            origin="provider",
                            created_at=now,
                            updated_at=now,
                        ),
                    )
                continue
            if ours.attempt_id is None or ours.status in TERMINAL_REFUND_STATES:
                continue
            try:
                target = move_refund(ours.status, snapshot.status)
            except IllegalTransition:  # includes stale: what the hub holds is newer
                continue
            if target is not ours.status:
                await repository.record_refund(
                    session,
                    refund_id=ours.id,
                    attempt_id=ours.attempt_id,
                    expected=(ours.status,),
                    status=target,
                    provider_refund_id=snapshot.provider_refund_id,
                    now=now,
                )

    # --- refunds --------------------------------------------------------------------------------

    async def refund(
        self, scope: ProjectScope, payment_id: str, *, amount: int | None, reason: str | None
    ) -> RefundView:
        """Refund ``amount`` minor units (``None``: everything still refundable)."""
        intent_id = _parse_payment_id(payment_id)
        if amount is not None and (type(amount) is not int or amount <= 0):
            raise _invalid("/amount", "must be a positive integer amount in minor units")
        try:
            clean_reason = validate_description(reason)
        except ValueError as error:
            raise _invalid("/reason", str(error)) from None
        refund_id, attempt_id = new_uuid(), new_uuid()
        async with self._uow() as uow:
            session = uow.session
            intent = await repository.get_intent(
                session, scope.project_id, intent_id, for_update=True
            )
            if intent is None:
                raise NotFound("Payment not found.")
            payment = await repository.get_provider_payment(session, intent.id, for_update=True)
            if payment is None or payment.state.capture_status is not CaptureStatus.CAPTURED:
                raise PaymentNotRefundable("Only a captured payment can be refunded.")
            if payment.state.dispute_status in (DisputeStatus.OPEN, DisputeStatus.LOST):
                raise PaymentNotRefundable("A disputed payment cannot be refunded.")
            existing = list(await repository.refunds_of(session, [intent.id]))
            pairs = _pairs(existing)
            captured = payment.state.captured_amount
            value = refundable_amount(captured, pairs) if amount is None else amount
            if value == 0:
                raise PaymentNotRefundable("Nothing is left to refund on this payment.")
            try:
                check_refund(value, captured, pairs)
            except RefundExceedsRefundable as error:
                raise _invalid(
                    "/amount", f"at most {error.refundable} can be refunded", "exceeds_refundable"
                ) from None
            provider = await self._resolve_provider(
                session, intent.project_id, intent.provider, intent.provider_account_id
            )
            now = self._clock.now()
            refund = Refund(
                id=refund_id,
                project_id=intent.project_id,
                payment_intent_id=intent.id,
                provider_payment_id=payment.id,
                amount=Money(value, intent.amount.currency),
                status=RefundState.REQUESTED,
                reason=clean_reason,
                provider_refund_id=None,
                attempt_id=attempt_id,
                origin="hub",
                created_at=now,
                updated_at=now,
            )
            await repository.insert_refund(session, refund)
            await self._ledger_hook(  # R8 step 1: the hold is booked before the provider call
                uow,
                PaymentChange(intent, payment, payment.state, now, existing, [*existing, refund]),
            )
            request = RefundRequest(
                provider_payment_id=payment.provider_payment_id,
                external_reference=format_id(IdPrefix.REFUND, refund_id),
                amount=refund.amount,
            )
            await self._insert_refund_attempt(session, refund, attempt_id, request, now)
            rf_id = format_id(IdPrefix.REFUND, refund_id)
            pi_id = format_id(IdPrefix.PAYMENT_INTENT, intent.id)
            await self._publish(
                uow,
                intent.project_id,
                payment_events.REFUND_REQUESTED.name,
                pi_id,
                refund_id=rf_id,
                payment_id=pi_id,
            )
            await AuditRecorder.record(
                session,
                actor=self._audit.acting(
                    Actor.of_principal(scope.principal_kind, scope.principal_id)
                ),
                action="refund.requested",
                target=AuditTarget("refund", rf_id),
                diff={
                    "payment_id": pi_id,
                    "amount": value,
                    "currency": intent.amount.currency.code,
                },
                project_id=intent.project_id,
            )
        await self._send_refund(provider, intent, refund, attempt_id, request)
        return await self._refund_view(intent.project_id, refund_id)

    @staticmethod
    async def _insert_refund_attempt(
        session: AsyncSession,
        refund: Refund,
        attempt_id: UUID,
        request: RefundRequest,
        now: datetime,
    ) -> None:
        digest = _request_hash(
            {
                "provider_payment_id": request.provider_payment_id,
                "external_reference": request.external_reference,
                "amount": request.amount.minor_units,
                "currency": request.amount.currency.code,
            }
        )
        await repository.insert_attempt(
            session,
            Attempt(
                id=attempt_id,
                project_id=refund.project_id,
                kind=AttemptKind.REFUND,
                payment_intent_id=refund.payment_intent_id,
                refund_id=refund.id,
                status=AttemptStatus.REQUESTED,
                request_hash=digest,
                created_at=now,
            ),
        )

    async def _refund_view(self, project_id: UUID, refund_id: UUID) -> RefundView:
        async with self._uow() as uow:
            refund = await repository.get_refund(uow.session, project_id, refund_id)
        if refund is None:  # pragma: no cover - read right after its own insert
            raise NotFound("Refund not found.")
        return _refund_view(refund)

    async def _send_refund(
        self,
        provider: PaymentProvider,
        intent: PaymentIntent,
        refund: Refund,
        attempt_id: UUID,
        request: RefundRequest,
    ) -> None:
        outcome = await self._call(provider.refund(request))
        if isinstance(outcome.result, ProviderRefundSnapshot):
            await self._record_refund(
                intent,
                refund,
                attempt_id,
                outcome.result.status,
                outcome.result.provider_refund_id,
                None,
            )
        elif isinstance(outcome.error, ProviderDuplicate):
            await self._lookup_refund(provider, intent, refund, attempt_id)
        elif isinstance(outcome.error, ProviderRejected):
            await self._record_refund(
                intent, refund, attempt_id, RefundState.DENIED, None, outcome.error.code
            )
        else:
            code = "unknown" if outcome.error is None else outcome.error.code
            await self._record_refund(intent, refund, attempt_id, RefundState.UNKNOWN, None, code)

    async def _lookup_refund(
        self, provider: PaymentProvider, intent: PaymentIntent, refund: Refund, attempt_id: UUID
    ) -> bool | None:
        """Find the refund in the payment's refund list. ``None``: confirmed absent."""
        async with self._uow() as uow:
            payment = await repository.get_provider_payment(uow.session, intent.id)
        if payment is None:  # pragma: no cover - a refund always has its provider payment
            return False
        outcome = await self._call(provider.get_payment(payment.provider_payment_id))
        if not isinstance(outcome.result, ProviderPaymentSnapshot):
            code = "unknown" if outcome.error is None else outcome.error.code
            await self._record_refund(intent, refund, attempt_id, RefundState.UNKNOWN, None, code)
            return False
        reference = format_id(IdPrefix.REFUND, refund.id)
        for snapshot in outcome.result.refunds:
            if snapshot.external_reference == reference:
                return await self._record_refund(
                    intent, refund, attempt_id, snapshot.status, snapshot.provider_refund_id, None
                )
        return None

    async def _record_refund(
        self,
        intent: PaymentIntent,
        refund: Refund,
        attempt_id: UUID,
        target: RefundState,
        provider_refund_id: str | None,
        code: str | None,
    ) -> bool:
        """Transaction B of a refund; ``False`` when the token is stale or the move is illegal."""
        async with self._uow() as uow:
            session = uow.session
            now = self._clock.now()
            today = self.today()
            current = await repository.get_refund(session, intent.project_id, refund.id)
            payment = await repository.get_provider_payment(session, intent.id, for_update=True)
            if current is None or payment is None:  # pragma: no cover - rows are never deleted
                return False
            try:
                status = move_refund(current.status, target)
            except IllegalTransition:
                status = None
            if status is current.status and current.attempt_id == attempt_id:
                return False  # a repeated outcome under the current token: nothing changes
            refunds = list(await repository.refunds_of(session, [intent.id]))
            before = derive_status(intent, payment, refunds, today)
            won = (
                status is not None
                and status is not current.status
                and (
                    await repository.record_refund(
                        session,
                        refund_id=refund.id,
                        attempt_id=attempt_id,
                        expected=_OPEN_REFUND,
                        status=status,
                        provider_refund_id=provider_refund_id,
                        now=now,
                    )
                )
            )
            outcome: dict[str, Any] = {} if code is None else {"code": code[:100]}
            if provider_refund_id is not None:
                outcome["provider_refund_id"] = provider_refund_id
            if not won:
                await repository.finish_attempt(
                    session, attempt_id, status=AttemptStatus.SUPERSEDED, outcome=outcome, now=now
                )
                return False
            if status is RefundState.UNKNOWN:
                await repository.mark_attempt_unknown(session, attempt_id, outcome=outcome)
            else:
                closed = (
                    AttemptStatus.REJECTED
                    if status is RefundState.DENIED
                    else (AttemptStatus.SUCCEEDED)
                )
                await repository.finish_attempt(
                    session, attempt_id, status=closed, outcome=outcome, now=now
                )
            recorded = list(await repository.refunds_of(session, [intent.id]))
            await self._ledger_hook(
                uow, PaymentChange(intent, payment, payment.state, now, refunds, recorded)
            )
            await self._status_changed(
                uow, intent, before, derive_status(intent, payment, recorded, today)
            )
        return True

    async def resolve_refund(self, refund_id: UUID) -> RefundState | None:
        """Converge one refund whose outcome is unknown (same rules as :meth:`resolve_payment`)."""
        async with self._uow() as uow:
            refund = await repository.find_refund(uow.session, refund_id)
            if refund is None or refund.status not in _OPEN_REFUND or refund.attempt_id is None:
                return None if refund is None else refund.status
            fresh = refund.updated_at > self._clock.now() - self._lease
            if refund.status is RefundState.REQUESTED and fresh:
                return refund.status
            intent = await repository.find_intent(uow.session, refund.payment_intent_id)
            if intent is None:  # pragma: no cover - foreign key
                return None
            try:
                provider = await self._provider(
                    uow.session, intent.project_id, intent.provider, intent.provider_account_id
                )
            except ProviderUnavailable as error:
                logger.warning("payments.resolver_provider_unavailable", code=error.code)
                return refund.status
        found = await self._lookup_refund(provider, intent, refund, refund.attempt_id)
        if found is None:
            await self._resend_refund(provider, intent, refund)
        async with self._uow() as uow:
            current = await repository.find_refund(uow.session, refund_id)
        return None if current is None else current.status

    async def _resend_refund(
        self, provider: PaymentProvider, intent: PaymentIntent, refund: Refund
    ) -> None:
        if refund.attempt_id is None:  # pragma: no cover - provider refunds are never resent
            return
        new_attempt = new_uuid()
        async with self._uow() as uow:
            session = uow.session
            now = self._clock.now()
            payment = await repository.get_provider_payment(session, intent.id)
            replaced = payment is not None and await repository.replace_refund_attempt(
                session,
                refund_id=refund.id,
                old_attempt_id=refund.attempt_id,
                new_attempt_id=new_attempt,
                now=now,
            )
            if payment is None or not replaced:
                return
            await repository.finish_attempt(
                session,
                refund.attempt_id,
                status=AttemptStatus.ABSENT,
                outcome={"lookup": "absent"},
                now=now,
            )
            request = RefundRequest(
                provider_payment_id=payment.provider_payment_id,
                external_reference=format_id(IdPrefix.REFUND, refund.id),
                amount=refund.amount,
            )
            await self._insert_refund_attempt(session, refund, new_attempt, request, now)
        await self._send_refund(provider, intent, refund, new_attempt, request)


def _with_creation(
    intent: PaymentIntent,
    status: CreationStatus,
    failure_code: str | None,
    attempt_id: UUID | None = None,
) -> PaymentIntent:
    return PaymentIntent(
        id=intent.id,
        project_id=intent.project_id,
        customer_id=intent.customer_id,
        amount=intent.amount,
        method=intent.method,
        due_date=intent.due_date,
        external_ref=intent.external_ref,
        description=intent.description,
        metadata=intent.metadata,
        provider=intent.provider,
        provider_account_id=intent.provider_account_id,
        creation_status=status,
        attempt_id=intent.attempt_id if attempt_id is None else attempt_id,
        failure_code=failure_code,
        created_at=intent.created_at,
        updated_at=intent.updated_at,
        invoice_id=intent.invoice_id,
    )


def _parse_payment_id(payment_id: str) -> UUID:
    try:
        return parse_id(payment_id, IdPrefix.PAYMENT_INTENT)
    except InvalidIdError:
        raise NotFound("Payment not found.") from None
