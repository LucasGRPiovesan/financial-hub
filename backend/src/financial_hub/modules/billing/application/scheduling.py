"""Worker wiring of billing: the period-close schedule and the invoice-paid handler."""

from typing import Final
from uuid import UUID

from financial_hub.modules.billing.application import events as billing_events
from financial_hub.modules.billing.application.config import BillingSettings
from financial_hub.modules.billing.application.service import BillingService
from financial_hub.modules.events.application import Event, HandlerRegistry, upsert_schedule
from financial_hub.modules.events.registry import registry as event_types
from financial_hub.platform.db import UnitOfWork
from financial_hub.shared.clock import Clock
from financial_hub.shared.ids import IdPrefix, InvalidIdError, parse_id

CLOSE_HANDLER: Final = "billing.period_close"
PAID_HANDLER: Final = "billing.invoice_paid"
CLOSE_SCHEDULE: Final = "billing.period_close"
PAYMENT_STATUS_CHANGED: Final = "payment.status_changed"
# The scheduler's own project (same as the payments sweeper's).
INTERNAL_PROJECT_ID: Final = UUID(int=0)


def register_billing_handlers(
    registry: HandlerRegistry, service: BillingService, settings: BillingSettings
) -> None:
    """The close runs after the dispatcher's transaction commits: each period is its own
    transaction (one ledger write each). Marking an invoice paid is a plain write, inline."""

    async def on_close_due(event: Event, uow: UnitOfWork) -> None:
        billing_events.CLOSE_DUE.parse(event.data)

        async def run() -> None:
            await service.close_due(limit=settings.close_batch_size)

        uow.after_commit.append(run)

    async def on_payment_status(event: Event, uow: UnitOfWork) -> None:
        payment_id = event_types.get(PAYMENT_STATUS_CHANGED).parse(event.data).payment_id
        try:
            intent_id = parse_id(payment_id, IdPrefix.PAYMENT_INTENT)
        except InvalidIdError:
            return
        await service.mark_paid(uow, intent_id)

    registry.register(CLOSE_HANDLER, on_close_due, event_types=[billing_events.CLOSE_DUE.name])
    registry.register(PAID_HANDLER, on_payment_status, event_types=[PAYMENT_STATUS_CHANGED])


async def ensure_close_schedule(uow: UnitOfWork, settings: BillingSettings, clock: Clock) -> UUID:
    """Create or update the schedule (disabled, not deleted, when the setting is off)."""
    return await upsert_schedule(
        uow,
        project_id=INTERNAL_PROJECT_ID,
        name=CLOSE_SCHEDULE,
        event_type=billing_events.CLOSE_DUE.name,
        interval_seconds=settings.close_interval_seconds,
        next_due_at=clock.now(),
        enabled=settings.close_enabled,
    )
