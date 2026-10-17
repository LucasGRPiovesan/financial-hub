"""Worker wiring of the daily reconciliation (off unless FH_RECONCILIATION_SCHEDULER_ENABLED)."""

from typing import Final
from uuid import UUID

from financial_hub.modules.events.application import Event, HandlerRegistry, upsert_schedule
from financial_hub.modules.reconciliation.application import events as reconciliation_events
from financial_hub.modules.reconciliation.application.config import ReconciliationSettings
from financial_hub.modules.reconciliation.application.service import ReconciliationService
from financial_hub.platform.db import UnitOfWork
from financial_hub.shared.clock import Clock

RUN_HANDLER: Final = "reconciliation.scheduled_run"
RUN_SCHEDULE: Final = "reconciliation.daily"
# The scheduler's own project (same as the payments sweeper's).
INTERNAL_PROJECT_ID: Final = UUID(int=0)


def register_reconciliation_handlers(
    registry: HandlerRegistry, service: ReconciliationService
) -> None:
    """The run only starts after the dispatcher's transaction commits (it calls the provider)."""

    async def on_due(event: Event, uow: UnitOfWork) -> None:
        reconciliation_events.RUN_DUE.parse(event.data)

        async def run() -> None:
            await service.run_scheduled()

        uow.after_commit.append(run)

    registry.register(RUN_HANDLER, on_due, event_types=[reconciliation_events.RUN_DUE.name])


async def ensure_reconciliation_schedule(
    uow: UnitOfWork, settings: ReconciliationSettings, clock: Clock
) -> UUID:
    """Create or update the schedule (disabled, not deleted, when the setting is off)."""
    return await upsert_schedule(
        uow,
        project_id=INTERNAL_PROJECT_ID,
        name=RUN_SCHEDULE,
        event_type=reconciliation_events.RUN_DUE.name,
        interval_seconds=settings.scheduler_interval_seconds,
        next_due_at=clock.now(),
        enabled=settings.scheduler_enabled,
    )
