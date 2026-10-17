"""Public API of reconciliation for the app and the worker (import-linter: only this file)."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from financial_hub.modules.audit.application import AuditContext
from financial_hub.modules.events.application import EventPublisher
from financial_hub.modules.payments.application.api import (
    ContactLoader,
    CredentialsLoader,
    PaymentsReconciliation,
    ProviderRegistry,
)
from financial_hub.modules.reconciliation.application.config import ReconciliationSettings
from financial_hub.modules.reconciliation.application.metrics import (
    ReconciliationMetrics,
    shared_metrics,
)
from financial_hub.modules.reconciliation.application.scheduling import (
    ensure_reconciliation_schedule,
    register_reconciliation_handlers,
)
from financial_hub.modules.reconciliation.application.service import (
    AdjustmentDecided,
    AdjustmentPayloadMismatch,
    AdjustmentPending,
    BreakClosed,
    FourEyesRequired,
    ReconciliationService,
)
from financial_hub.modules.reconciliation.domain import Adjustment, Break, Run
from financial_hub.shared.clock import Clock

__all__ = [
    "Adjustment",
    "AdjustmentDecided",
    "AdjustmentPayloadMismatch",
    "AdjustmentPending",
    "Break",
    "BreakClosed",
    "FourEyesRequired",
    "ReconciliationMetrics",
    "ReconciliationService",
    "ReconciliationSettings",
    "Run",
    "build_reconciliation_service",
    "ensure_reconciliation_schedule",
    "register_reconciliation_handlers",
    "shared_metrics",
]


def build_reconciliation_service(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    clock: Clock,
    registry: ProviderRegistry,
    credentials: CredentialsLoader | None = None,
    contacts: ContactLoader | None = None,
    audit: AuditContext | None = None,
    publisher: EventPublisher | None = None,
) -> ReconciliationService:
    payments = PaymentsReconciliation(
        session_factory,
        clock=clock,
        registry=registry,
        credentials=credentials,
        contacts=contacts,
    )
    return ReconciliationService(
        session_factory, clock=clock, payments=payments, audit=audit, publisher=publisher
    )
