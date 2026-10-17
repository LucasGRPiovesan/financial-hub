"""What reconciliation (ADR-0013) needs from payments: hub-side records and provider-side reads.

Plain views in integer minor units, so the reconciliation module never sees payments' domain or
tables. Provider calls (``list_payments``, ``get_payment``, ``get_balance``) must be made outside
any unit of work; :meth:`PaymentsReconciliation.reader` only builds the client.
"""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol, cast
from uuid import UUID, uuid5

from sqlalchemy import ColumnElement, and_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from financial_hub.modules.audit.application import AuditContext
from financial_hub.modules.payments.application.ledger_postings import (
    SURROGATE_ACCOUNT_NAMESPACE,
    booked_net,
)
from financial_hub.modules.payments.application.registry import (
    ContactLoader,
    CredentialsLoader,
    ProviderRegistry,
)
from financial_hub.modules.payments.application.service import PaymentService
from financial_hub.modules.payments.domain import (
    FAKE_PROVIDER,
    CaptureStatus,
    DisputeStatus,
    PaymentProvider,
    ProviderError,
    ProviderPaymentSnapshot,
    ProviderReconciliationReader,
    ProviderRejected,
    ProviderUnavailable,
    RefundState,
)
from financial_hub.modules.payments.infrastructure import repository
from financial_hub.modules.payments.infrastructure.tables import payment_intents, provider_payments
from financial_hub.shared.clock import Clock
from financial_hub.shared.ids import IdPrefix, InvalidIdError, format_id, parse_id

__all__ = [
    "ExternalPayment",
    "InternalPayment",
    "PaymentsReconciliation",
    "ProviderBalanceView",
    "ProviderReadError",
    "ReconciliationReader",
    "ReconciliationTarget",
]


class ProviderReadError(Exception):
    """A provider read failed (``code`` is the provider error code, never a secret)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ReconciliationTarget:
    """One provider account of one project the hub has payments on."""

    project_id: UUID
    provider: str
    provider_account_id: UUID | None


@dataclass(frozen=True, slots=True)
class InternalPayment:
    """The hub's view of one payment (an intent with a provider payment)."""

    intent_id: UUID
    external_reference: str
    provider_payment_id: str
    currency: str
    amount: int
    capture_status: str
    settlement_status: str
    dispute_status: str
    booked_net: int | None
    refunded: int
    standalone: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ExternalPayment:
    """The provider's view of one payment; ``net_amount`` is ``None`` when it reports none."""

    provider_payment_id: str
    external_reference: str | None
    currency: str
    amount: int
    capture_status: str
    settlement_status: str
    dispute_status: str
    net_amount: int | None
    refunded: int


@dataclass(frozen=True, slots=True)
class ProviderBalanceView:
    currency: str
    amount: int


class ReconciliationReader(Protocol):
    """The provider-side reads of one account (built by :meth:`PaymentsReconciliation.reader`)."""

    async def list_payments(self, created_from: date, created_to: date) -> list[ExternalPayment]:
        """Payments created on ``created_from..created_to`` (inclusive)."""
        ...

    async def get_payment(self, provider_payment_id: str) -> ExternalPayment | None: ...

    async def get_balance(self) -> ProviderBalanceView: ...


def _external(snapshot: ProviderPaymentSnapshot) -> ExternalPayment:
    refunded = sum(
        refund.amount.minor_units
        for refund in snapshot.refunds
        if refund.status is RefundState.DONE
    )
    return ExternalPayment(
        provider_payment_id=snapshot.provider_payment_id,
        external_reference=snapshot.external_reference,
        currency=snapshot.amount.currency.code,
        amount=snapshot.amount.minor_units,
        capture_status=snapshot.capture_status.value,
        settlement_status=snapshot.settlement_status.value,
        dispute_status=snapshot.dispute_status.value,
        net_amount=snapshot.net_amount,
        refunded=refunded,
    )


class _Reader:
    def __init__(self, provider: PaymentProvider, listing: ProviderReconciliationReader) -> None:
        self._provider = provider
        self._listing = listing

    async def list_payments(self, created_from: date, created_to: date) -> list[ExternalPayment]:
        try:
            found = await self._listing.list_payments(created_from, created_to)
        except ProviderError as error:
            raise ProviderReadError(error.code) from None
        return [_external(snapshot) for snapshot in found]

    async def get_payment(self, provider_payment_id: str) -> ExternalPayment | None:
        """The payment, or ``None`` when the provider definitively says it does not exist."""
        try:
            return _external(await self._provider.get_payment(provider_payment_id))
        except ProviderRejected as error:
            if error.code == "not_found":
                return None
            raise ProviderReadError(error.code) from None
        except ProviderError as error:
            raise ProviderReadError(error.code) from None

    async def get_balance(self) -> ProviderBalanceView:
        try:
            balance = await self._listing.get_balance()
        except ProviderError as error:
            raise ProviderReadError(error.code) from None
        return ProviderBalanceView(balance.amount.currency.code, balance.amount.minor_units)


class PaymentsReconciliation:
    """Payments facade for reconciliation; reads run on the caller's session."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Clock,
        registry: ProviderRegistry,
        credentials: CredentialsLoader | None = None,
        contacts: ContactLoader | None = None,
    ) -> None:
        self._registry = registry
        self._credentials = credentials
        self._service = PaymentService(
            session_factory,
            clock=clock,
            registry=registry,
            credentials=credentials,
            contacts=contacts,
            audit=AuditContext.system(),
        )

    # --- hub side -------------------------------------------------------------------------------

    @staticmethod
    async def targets(session: AsyncSession) -> list[ReconciliationTarget]:
        """Every (project, provider, account) that has at least one provider payment."""
        statement = select(
            provider_payments.c.project_id,
            provider_payments.c.provider,
            provider_payments.c.provider_account_id,
        ).group_by(
            provider_payments.c.project_id,
            provider_payments.c.provider,
            provider_payments.c.provider_account_id,
        )
        rows = (await session.execute(statement)).all()
        return [ReconciliationTarget(row[0], row[1], row[2]) for row in rows]

    @staticmethod
    def ledger_provider_account(project_id: UUID, provider: str, account_id: UUID | None) -> UUID:
        """The owner id of ``provider_balance:{pa}`` (the fake's surrogate when no account)."""
        if account_id is not None:
            return account_id
        return uuid5(SURROGATE_ACCOUNT_NAMESPACE, f"{provider}:{project_id}")

    @staticmethod
    async def internal_in_window(
        session: AsyncSession,
        target: ReconciliationTarget,
        created_from: datetime,
        created_until: datetime,
    ) -> list[InternalPayment]:
        """Payments of ``target`` whose intent was created in ``[created_from, created_until)``."""
        return await _internal(
            session,
            target,
            and_(
                payment_intents.c.created_at >= created_from,
                payment_intents.c.created_at < created_until,
            ),
        )

    @staticmethod
    async def internal_by_provider_ids(
        session: AsyncSession, target: ReconciliationTarget, provider_payment_ids: Collection[str]
    ) -> list[InternalPayment]:
        if not provider_payment_ids:
            return []
        condition = provider_payments.c.provider_payment_id.in_(list(provider_payment_ids))
        return await _internal(session, target, condition)

    @staticmethod
    async def internal_by_references(
        session: AsyncSession, target: ReconciliationTarget, references: Collection[str]
    ) -> list[InternalPayment]:
        """Payments whose hub reference (``pi_...``) is one of ``references`` (others ignored)."""
        ids: list[UUID] = []
        for reference in references:
            try:
                ids.append(parse_id(reference, IdPrefix.PAYMENT_INTENT))
            except InvalidIdError:
                continue
        if not ids:
            return []
        return await _internal(session, target, payment_intents.c.id.in_(ids))

    @staticmethod
    async def settled_standalone_intents(
        session: AsyncSession, project_id: UUID, intent_ids: Collection[UUID]
    ) -> list[UUID]:
        """Of ``intent_ids``: standalone, captured, never refunded or disputed (clearing is 0)."""
        if not intent_ids:
            return []
        statement = (
            select(payment_intents.c.id)
            .join(provider_payments, provider_payments.c.payment_intent_id == payment_intents.c.id)
            .where(
                payment_intents.c.project_id == project_id,
                payment_intents.c.id.in_(list(intent_ids)),
                payment_intents.c.invoice_id.is_(None),
                provider_payments.c.capture_status == CaptureStatus.CAPTURED.value,
                provider_payments.c.dispute_status == DisputeStatus.NONE.value,
            )
        )
        candidates = [row[0] for row in (await session.execute(statement)).all()]
        refunded = {
            refund.payment_intent_id for refund in await repository.refunds_of(session, candidates)
        }
        return [intent_id for intent_id in candidates if intent_id not in refunded]

    # --- provider side --------------------------------------------------------------------------

    async def reader(
        self, session: AsyncSession, target: ReconciliationTarget
    ) -> ReconciliationReader:
        """The account's read client; :class:`ProviderReadError` when it has no listing."""
        credentials = None
        account_id = target.provider_account_id
        if (
            target.provider != FAKE_PROVIDER
            and account_id is not None
            and self._credentials is not None
        ):
            credentials = await self._credentials(session, target.project_id, account_id)
        try:
            provider = self._registry.resolve(target.provider, account_id, credentials)
        except ProviderUnavailable as error:
            raise ProviderReadError(error.code) from None
        if not isinstance(provider, ProviderReconciliationReader):
            raise ProviderReadError("reconciliation_unsupported")
        return _Reader(provider, cast("ProviderReconciliationReader", provider))

    async def refresh(self, intent_id: UUID) -> None:
        """Re-fetch the payment and apply it through the normal transitions (auto-resolution)."""
        await self._service.refresh(intent_id)


async def _internal(
    session: AsyncSession, target: ReconciliationTarget, condition: ColumnElement[bool]
) -> list[InternalPayment]:
    account = provider_payments.c.provider_account_id
    statement = (
        select(payment_intents.c.id)
        .join(provider_payments, provider_payments.c.payment_intent_id == payment_intents.c.id)
        .where(
            payment_intents.c.project_id == target.project_id,
            provider_payments.c.provider == target.provider,
            account.is_(None)
            if target.provider_account_id is None
            else account == target.provider_account_id,
            condition,
        )
    )
    intent_ids = [row[0] for row in (await session.execute(statement)).all()]
    if not intent_ids:
        return []
    payments = await repository.provider_payments_for(session, intent_ids)
    refunds: dict[UUID, int] = {}
    for refund in await repository.refunds_of(session, intent_ids):
        if refund.status is RefundState.DONE:
            refunds[refund.payment_intent_id] = (
                refunds.get(refund.payment_intent_id, 0) + refund.amount.minor_units
            )
    found: list[InternalPayment] = []
    for intent_id in intent_ids:
        intent = await repository.find_intent(session, intent_id)
        payment = payments.get(intent_id)
        if intent is None or payment is None:  # pragma: no cover - joined above
            continue
        state = payment.state
        captured = state.capture_status is CaptureStatus.CAPTURED
        found.append(
            InternalPayment(
                intent_id=intent.id,
                external_reference=format_id(IdPrefix.PAYMENT_INTENT, intent.id),
                provider_payment_id=payment.provider_payment_id,
                currency=intent.amount.currency.code,
                amount=intent.amount.minor_units,
                capture_status=state.capture_status.value,
                settlement_status=state.settlement_status.value,
                dispute_status=state.dispute_status.value,
                booked_net=booked_net(state) if captured else None,
                refunded=refunds.get(intent_id, 0),
                standalone=intent.invoice_id is None,
                created_at=intent.created_at,
            )
        )
    return found
