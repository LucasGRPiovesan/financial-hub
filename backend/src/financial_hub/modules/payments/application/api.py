"""Public API of the payments module for other modules (import-linter: only this file)."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from financial_hub.modules.payments.application.config import PaymentsSettings
from financial_hub.modules.payments.application.ledger_postings import post_payment_changes
from financial_hub.modules.payments.application.reconciliation import (
    ExternalPayment,
    InternalPayment,
    PaymentsReconciliation,
    ProviderBalanceView,
    ProviderReadError,
    ReconciliationReader,
    ReconciliationTarget,
)
from financial_hub.modules.payments.application.registry import (
    ContactLoader,
    CredentialsLoader,
    ProviderRegistry,
    build_registry,
    customers_contact_loader,
    projects_credentials_loader,
)
from financial_hub.modules.payments.application.service import (
    NewPayment,
    PaymentService,
    PaymentView,
    PendingPayment,
    RefundView,
)
from financial_hub.modules.payments.application.webhooks import (
    WebhookInbox,
    WebhookProcessor,
    WebhookReceipt,
    ensure_sweeper_schedule,
    register_payment_handlers,
)
from financial_hub.modules.payments.domain import CaptureStatus
from financial_hub.modules.payments.infrastructure import repository

__all__ = [
    "ContactLoader",
    "CredentialsLoader",
    "ExternalPayment",
    "InternalPayment",
    "NewPayment",
    "PaymentService",
    "PaymentView",
    "PaymentsReconciliation",
    "PaymentsSettings",
    "PendingPayment",
    "ProviderBalanceView",
    "ProviderReadError",
    "ProviderRegistry",
    "ReconciliationReader",
    "ReconciliationTarget",
    "RefundView",
    "WebhookInbox",
    "WebhookProcessor",
    "WebhookReceipt",
    "build_registry",
    "customers_contact_loader",
    "ensure_sweeper_schedule",
    "is_captured",
    "post_payment_changes",
    "projects_credentials_loader",
    "register_payment_handlers",
]


async def is_captured(session: AsyncSession, intent_id: UUID) -> bool:
    """Whether the intent's provider payment is captured (billing marks its invoice paid)."""
    payment = await repository.get_provider_payment(session, intent_id)
    return payment is not None and payment.state.capture_status is CaptureStatus.CAPTURED
