"""Inbound provider webhooks and the payments sweeper (W4.4, docs/INTEGRATIONS.md s2).

Receiving (the HTTP request) only authenticates, stores and enqueues:

1. the account named in the URL must exist, be active and belong to the provider, and the
   presented token must match the account's ``webhook_token`` credential (constant-time compare
   of SHA-256 digests, so neither content nor length leaks). Every failure is the same 401;
2. the raw body is stored in ``provider_webhook_events`` with the provider's event id as the
   dedup key. A duplicate stores nothing and enqueues nothing;
3. a new row publishes the internal ``payments.webhook_received`` event in the same transaction.

Processing runs in the worker: the handler registers an after-commit hook (handlers must not call
providers) that refetches the payment through :meth:`PaymentService.refresh`. The notification
body only says *which* payment changed; amounts and statuses always come from the refetch, so a
forged body, a replay or an out-of-order delivery cannot move money (ADR-0012 s2). A row that
fails (provider down, creation still in flight) is retried by the sweeper.

Internal events are published under the nil project id: no customer webhook endpoint (always
subscribed per project) and no project's realtime stream ever sees them.
"""

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Final
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from financial_hub.modules.events.application import (
    Event,
    EventDraft,
    EventPublisher,
    HandlerRegistry,
    upsert_schedule,
)
from financial_hub.modules.payments.application import events as payment_events
from financial_hub.modules.payments.application.config import PaymentsSettings
from financial_hub.modules.payments.application.registry import CredentialsLoader
from financial_hub.modules.payments.application.service import PaymentService
from financial_hub.modules.payments.domain import (
    PENDING_WEBHOOK_STATES,
    CreationStatus,
    InboundWebhook,
    ParsedNotification,
    PaymentIntent,
    WebhookAction,
    WebhookPayloadInvalid,
    WebhookStatus,
)
from financial_hub.modules.payments.infrastructure import repository, webhook_repository
from financial_hub.modules.payments.infrastructure.asaas_provider import (
    PROVIDER_NAME as ASAAS_PROVIDER,
)
from financial_hub.modules.payments.infrastructure.asaas_webhooks import parse_notification
from financial_hub.modules.projects.application import api as projects_api
from financial_hub.platform.db import UnitOfWork
from financial_hub.shared.clock import Clock
from financial_hub.shared.errors import Unauthorized, ValidationFailed
from financial_hub.shared.ids import IdPrefix, InvalidIdError, new_uuid, parse_id

logger = structlog.get_logger()

INTERNAL_PROJECT_ID: Final = UUID(int=0)
WEBHOOK_HANDLER: Final = "payments.webhooks"
SWEEPER_HANDLER: Final = "payments.sweeper"
SWEEPER_SCHEDULE: Final = "payments.sweeper"
# Shorter configured tokens are treated as absent (every request is refused): a guessable token
# would let anyone enqueue refetches.
MIN_WEBHOOK_TOKEN_LENGTH: Final = 32
TOKEN_CREDENTIAL: Final = "webhook_token"  # noqa: S105 - a credential field name
REJECTED: Final = "Invalid webhook credentials."
SUPPORTED_PROVIDERS: Final = frozenset({ASAAS_PROVIDER})

# Compared against when the account has no usable token, so a refusal costs the same work.
_ABSENT_TOKEN_DIGEST: Final = hashlib.sha256(secrets.token_bytes(32)).digest()


def token_matches(presented: str | None, expected: str | None) -> bool:
    """Constant-time check of a presented webhook token (``expected=None``: never matches)."""
    presented_digest = hashlib.sha256((presented or "").encode("utf-8", "replace")).digest()
    if expected is None:
        hmac.compare_digest(presented_digest, _ABSENT_TOKEN_DIGEST)
        return False
    expected_digest = hashlib.sha256(expected.encode("utf-8", "replace")).digest()
    return hmac.compare_digest(presented_digest, expected_digest) and bool(presented)


@dataclass(frozen=True, slots=True)
class WebhookReceipt:
    webhook_id: UUID | None  # ``None`` for a duplicate delivery
    event_type: str
    duplicate: bool


class _Retry(Exception):
    """Processing could not finish now; the sweeper retries the row later."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# --- receiving ----------------------------------------------------------------------------------


class WebhookInbox:
    """Authenticate, store and enqueue one inbound notification (the HTTP request's only work)."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Clock,
        credentials: CredentialsLoader | None,
        publisher: EventPublisher | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._credentials = credentials
        self._publisher = publisher or EventPublisher(clock=clock, notifier=None)

    async def receive(
        self, provider: str, account_id: str, presented_token: str | None, body: bytes
    ) -> WebhookReceipt:
        async with UnitOfWork(self._session_factory) as uow:
            session = uow.session
            account = await self._authenticate(session, provider, account_id, presented_token)
            notification = _parsed(body)
            now = self._clock.now()
            webhook = InboundWebhook(
                id=new_uuid(),
                project_id=account.project_id,
                provider=provider,
                provider_account_id=account.id,
                provider_event_id=notification.provider_event_id,
                event_type=notification.event_type,
                body=body.decode("utf-8"),
                status=WebhookStatus.RECEIVED,
                outcome=None,
                attempts=0,
                last_error=None,
                received_at=now,
                processed_at=None,
                updated_at=now,
            )
            if not await webhook_repository.insert_webhook(session, webhook):
                logger.info(
                    "payments.webhook_duplicate",
                    provider=provider,
                    event_type=notification.event_type,
                )
                return WebhookReceipt(None, notification.event_type, duplicate=True)
            await self._publisher.publish(
                uow,
                EventDraft(
                    type=payment_events.WEBHOOK_RECEIVED.name,
                    data={"webhook_id": str(webhook.id)},
                    project_id=INTERNAL_PROJECT_ID,
                    subject=f"webhook:{webhook.id}",
                ),
            )
        logger.info(
            "payments.webhook_received",
            provider=provider,
            webhook_id=str(webhook.id),
            event_type=notification.event_type,
        )
        return WebhookReceipt(webhook.id, notification.event_type, duplicate=False)

    async def _authenticate(
        self, session: AsyncSession, provider: str, raw_account_id: str, presented: str | None
    ) -> projects_api.ProviderAccountRef:
        account: projects_api.ProviderAccountRef | None = None
        expected: str | None = None
        try:
            account_id = parse_id(raw_account_id, IdPrefix.PROVIDER_ACCOUNT)
        except InvalidIdError:
            account_id = None
        if account_id is not None and provider in SUPPORTED_PROVIDERS:
            account = await projects_api.provider_account_by_id(session, account_id)
        if account is not None and account.active and account.provider == provider:
            loaded = (
                None
                if self._credentials is None
                else await self._credentials(session, account.project_id, account.id)
            )
            expected = None if loaded is None else loaded.get(TOKEN_CREDENTIAL)
            if expected is not None and len(expected) < MIN_WEBHOOK_TOKEN_LENGTH:
                logger.warning("payments.webhook_token_too_short", provider=provider)
                expected = None
        if not token_matches(presented, expected) or account is None:
            logger.info("payments.webhook_rejected", provider=provider)
            raise Unauthorized(REJECTED)
        return account


def _parsed(body: bytes) -> ParsedNotification:
    try:
        return parse_notification(body.decode("utf-8"))
    except UnicodeDecodeError, WebhookPayloadInvalid:
        raise ValidationFailed("The body is not a provider notification.") from None


# --- processing ---------------------------------------------------------------------------------


class WebhookProcessor:
    """Apply stored notifications by refetching (worker side), and run the periodic sweep."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        service: PaymentService,
        *,
        clock: Clock,
        settings: PaymentsSettings | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._service = service
        self._clock = clock
        self._settings = settings or PaymentsSettings()

    def _uow(self) -> UnitOfWork:
        return UnitOfWork(self._session_factory)

    async def process(self, webhook_id: UUID) -> WebhookStatus | None:
        """Process one row; a finished row is left alone (``None`` for an unknown id)."""
        async with self._uow() as uow:
            webhook = await webhook_repository.get_webhook(uow.session, webhook_id)
        if webhook is None:
            return None
        if webhook.status not in PENDING_WEBHOOK_STATES:
            return webhook.status
        try:
            status, outcome = await self._handle(webhook)
        except Exception as error:  # recorded and retried; never lost
            code = error.code if isinstance(error, _Retry) else type(error).__name__
            async with self._uow() as uow:
                await webhook_repository.fail_webhook(
                    uow.session, webhook.id, error=code, now=self._clock.now()
                )
            logger.warning(
                "payments.webhook_failed",
                webhook_id=str(webhook.id),
                event_type=webhook.event_type,
                error=code[:100],
            )
            return WebhookStatus.FAILED
        async with self._uow() as uow:
            await webhook_repository.finish_webhook(
                uow.session, webhook.id, status=status, outcome=outcome, now=self._clock.now()
            )
        logger.info(
            "payments.webhook_processed",
            webhook_id=str(webhook.id),
            event_type=webhook.event_type,
            status=status.value,
            outcome=outcome,
        )
        return status

    async def _handle(self, webhook: InboundWebhook) -> tuple[WebhookStatus, str]:
        try:
            notification = parse_notification(webhook.body)
        except WebhookPayloadInvalid:  # pragma: no cover - rows are parsed before they are stored
            return WebhookStatus.PARKED, "invalid_body"
        if notification.action is WebhookAction.IGNORE:
            return WebhookStatus.IGNORED, "ignored_event_type"
        payment_id = notification.provider_payment_id
        if notification.action is WebhookAction.PARK or payment_id is None:
            return WebhookStatus.PARKED, "unsupported_event_type"
        intent, known_payment_id = await self._intent_of(
            webhook, payment_id, notification.external_reference
        )
        if intent is None:
            return WebhookStatus.IGNORED, "unknown_payment"
        if not _bound(webhook, intent):
            logger.warning("payments.webhook_binding_mismatch", webhook_id=str(webhook.id))
            return WebhookStatus.PARKED, "binding_mismatch"
        if known_payment_id is not None and known_payment_id != payment_id:
            logger.warning("payments.webhook_payment_mismatch", webhook_id=str(webhook.id))
            return WebhookStatus.PARKED, "payment_mismatch"
        if known_payment_id is None:
            # the creation outcome is not recorded yet (the notification raced the response, or
            # the outcome is unknown): converge it first, then refetch like any other event
            creation = await self._service.resolve_payment(intent.id)
            if creation in (CreationStatus.REJECTED, None):
                # the hub recorded that nothing exists there: a person must look at it
                logger.warning("payments.webhook_for_rejected_creation", webhook_id=str(webhook.id))
                return WebhookStatus.PARKED, "creation_rejected"
        status = await self._service.refresh(intent.id)
        if status is None:
            raise _Retry("creation_pending" if known_payment_id is None else "refetch_failed")
        return WebhookStatus.PROCESSED, f"refreshed:{status.value}"

    async def _intent_of(
        self, webhook: InboundWebhook, payment_id: str, reference: str | None
    ) -> tuple[PaymentIntent | None, str | None]:
        """The intent the notification names, and its recorded provider payment id if any."""
        async with self._uow() as uow:
            session = uow.session
            intent = await repository.find_intent_by_provider_payment(
                session,
                provider=webhook.provider,
                provider_account_id=webhook.provider_account_id,
                provider_payment_id=payment_id,
            )
            if intent is None and reference is not None:
                try:
                    intent_id = parse_id(reference, IdPrefix.PAYMENT_INTENT)
                except InvalidIdError:
                    return None, None
                intent = await repository.find_intent(session, intent_id)
            if intent is None:
                return None, None
            payment = await repository.get_provider_payment(session, intent.id)
        return intent, None if payment is None else payment.provider_payment_id

    async def sweep(self) -> None:
        """Resolve unknown outcomes, then retry webhook rows nobody finished."""
        batch = self._settings.sweeper_batch_size
        looked_at = await self._service.resolve_pending(limit=batch)
        cutoff = self._clock.now() - timedelta(seconds=self._settings.webhook_retry_after_seconds)
        async with self._uow() as uow:
            pending = await webhook_repository.pending_webhooks(
                uow.session,
                untouched_before=cutoff,
                max_attempts=self._settings.webhook_max_attempts,
                limit=batch,
            )
        for webhook_id in pending:
            await self.process(webhook_id)
        logger.info("payments.sweep_done", resolved=looked_at, webhooks=len(pending))


def _bound(webhook: InboundWebhook, intent: PaymentIntent) -> bool:
    """The notification's account (authenticated by its token) owns the payment it names."""
    return (
        intent.project_id == webhook.project_id
        and intent.provider == webhook.provider
        and intent.provider_account_id == webhook.provider_account_id
    )


# --- worker wiring ------------------------------------------------------------------------------


def register_payment_handlers(registry: HandlerRegistry, processor: WebhookProcessor) -> None:
    """The two worker handlers. Both only queue an after-commit hook: no provider call runs
    inside the dispatcher's transaction (a lost hook is caught up by the sweeper)."""

    async def on_webhook(event: Event, uow: UnitOfWork) -> None:
        webhook_id = UUID(payment_events.WEBHOOK_RECEIVED.parse(event.data).webhook_id)

        async def run() -> None:
            await processor.process(webhook_id)

        uow.after_commit.append(run)

    async def on_sweep(event: Event, uow: UnitOfWork) -> None:
        payment_events.SWEEP_DUE.parse(event.data)
        uow.after_commit.append(processor.sweep)

    registry.register(
        WEBHOOK_HANDLER, on_webhook, event_types=[payment_events.WEBHOOK_RECEIVED.name]
    )
    registry.register(SWEEPER_HANDLER, on_sweep, event_types=[payment_events.SWEEP_DUE.name])


async def ensure_sweeper_schedule(
    uow: UnitOfWork, settings: PaymentsSettings, clock: Clock
) -> UUID:
    """Create or update the sweeper schedule (disabled, not deleted, when the setting is off)."""
    return await upsert_schedule(
        uow,
        project_id=INTERNAL_PROJECT_ID,
        name=SWEEPER_SCHEDULE,
        event_type=payment_events.SWEEP_DUE.name,
        interval_seconds=settings.sweeper_interval_seconds,
        next_due_at=clock.now(),
        enabled=settings.sweeper_enabled,
    )
