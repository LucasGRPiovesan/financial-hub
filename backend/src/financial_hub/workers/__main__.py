"""Worker process: event dispatcher, scheduler and webhook sender (ADR-0005), plus a heartbeat.

SIGINT/SIGTERM set the stop event: the dispatcher releases leases it has not started, the
scheduler gives up leadership, and the process exits well inside the compose grace period.
"""

import asyncio
import signal
from pathlib import Path

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from financial_hub.modules.billing.application.api import (
    BillingService,
    BillingSettings,
    ensure_close_schedule,
    register_billing_handlers,
)
from financial_hub.modules.events.application import EventPublisher, build_runtime, handlers
from financial_hub.modules.payments.application.api import (
    PaymentService,
    PaymentsSettings,
    WebhookProcessor,
    build_registry,
    customers_contact_loader,
    ensure_sweeper_schedule,
    projects_credentials_loader,
    register_payment_handlers,
)
from financial_hub.modules.reconciliation.application.api import (
    ReconciliationSettings,
    build_reconciliation_service,
    ensure_reconciliation_schedule,
    register_reconciliation_handlers,
)
from financial_hub.modules.webhooks.application.api import WebhookSettings, build_sender
from financial_hub.platform.crypto.keyring import Keyring
from financial_hub.platform.db import UnitOfWork, create_engine, create_session_factory
from financial_hub.platform.logging import configure_logging
from financial_hub.platform.settings import get_settings
from financial_hub.platform.telemetry import configure_telemetry, instrument_engine
from financial_hub.shared.clock import SystemClock

HEARTBEAT_FILE = Path("/tmp/worker.heartbeat")  # noqa: S108 - container-local liveness marker
HEARTBEAT_INTERVAL_SECONDS = 15

logger = structlog.get_logger()


async def heartbeat(stop: asyncio.Event) -> None:
    while not stop.is_set():
        await asyncio.to_thread(HEARTBEAT_FILE.touch)
        try:
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_INTERVAL_SECONDS)
        except TimeoutError:
            continue


def payment_processor(
    session_factory: async_sessionmaker[AsyncSession],
    publisher: EventPublisher,
    clock: SystemClock,
    settings: PaymentsSettings,
) -> WebhookProcessor:
    """Inbound webhooks and the sweeper (W4.4): the worker refetches, never the API request."""
    app_settings = get_settings()
    keyring = Keyring.from_settings(app_settings)
    service = PaymentService(
        session_factory,
        clock=clock,
        registry=build_registry(app_settings),
        credentials=projects_credentials_loader(keyring),
        contacts=customers_contact_loader(keyring),
        publisher=publisher,
    )
    return WebhookProcessor(session_factory, service, clock=clock, settings=settings)


def billing_service(
    session_factory: async_sessionmaker[AsyncSession], publisher: EventPublisher, clock: SystemClock
) -> BillingService:
    """Period close and invoice payments (W5.3); invoice intents are sent after each commit."""
    app_settings = get_settings()
    keyring = Keyring.from_settings(app_settings)
    payments = PaymentService(
        session_factory,
        clock=clock,
        registry=build_registry(app_settings),
        credentials=projects_credentials_loader(keyring),
        contacts=customers_contact_loader(keyring),
        publisher=publisher,
    )
    return BillingService(session_factory, clock=clock, payments=payments, publisher=publisher)


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    telemetry = configure_telemetry(settings)
    engine = create_engine(settings)
    if telemetry is not None:
        instrument_engine(engine, telemetry)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    logger.info("worker.started")
    try:
        runtime = build_runtime(engine)
        webhook_settings = WebhookSettings()
        payments_settings = PaymentsSettings()
        processor = payment_processor(
            create_session_factory(engine), runtime.publisher, SystemClock(), payments_settings
        )
        register_payment_handlers(handlers, processor)
        keyring = Keyring.from_settings(settings)
        reconciliation_settings = ReconciliationSettings()
        reconciliation = build_reconciliation_service(
            create_session_factory(engine),
            clock=SystemClock(),
            registry=build_registry(settings),
            credentials=projects_credentials_loader(keyring),
            contacts=customers_contact_loader(keyring),
            publisher=runtime.publisher,
        )
        register_reconciliation_handlers(handlers, reconciliation)
        billing_settings = BillingSettings()
        billing = billing_service(create_session_factory(engine), runtime.publisher, SystemClock())
        register_billing_handlers(handlers, billing, billing_settings)
        async with UnitOfWork(create_session_factory(engine)) as uow:
            await runtime.sync_subscriptions(uow)
            await ensure_sweeper_schedule(uow, payments_settings, SystemClock())
            await ensure_reconciliation_schedule(uow, reconciliation_settings, SystemClock())
            await ensure_close_schedule(uow, billing_settings, SystemClock())
        async with asyncio.TaskGroup() as group:
            group.create_task(heartbeat(stop))
            group.create_task(runtime.dispatcher.run(stop))
            group.create_task(runtime.scheduler.run(stop))
            if webhook_settings.sender_enabled:
                sender = build_sender(
                    create_session_factory(engine),
                    settings=settings,
                    keyring=Keyring.from_settings(settings),
                    clock=SystemClock(),
                    webhook_settings=webhook_settings,
                )
                group.create_task(sender.run(stop))
    finally:
        await engine.dispose()
        if telemetry is not None:
            telemetry.shutdown()
        logger.info("worker.stopped")


if __name__ == "__main__":
    asyncio.run(run())
