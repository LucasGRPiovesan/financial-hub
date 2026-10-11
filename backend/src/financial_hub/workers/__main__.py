"""Worker process: event dispatcher + scheduler (ADR-0005), with a liveness heartbeat file.

SIGINT/SIGTERM set the stop event: the dispatcher releases leases it has not started, the
scheduler gives up leadership, and the process exits well inside the compose grace period.
"""

import asyncio
import signal
from pathlib import Path

import structlog

from financial_hub.modules.events.application import build_runtime
from financial_hub.platform.db import UnitOfWork, create_engine, create_session_factory
from financial_hub.platform.logging import configure_logging
from financial_hub.platform.settings import get_settings
from financial_hub.platform.telemetry import configure_telemetry, instrument_engine

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
        async with UnitOfWork(create_session_factory(engine)) as uow:
            await runtime.sync_subscriptions(uow)
        async with asyncio.TaskGroup() as group:
            group.create_task(heartbeat(stop))
            group.create_task(runtime.dispatcher.run(stop))
            group.create_task(runtime.scheduler.run(stop))
    finally:
        await engine.dispose()
        if telemetry is not None:
            telemetry.shutdown()
        logger.info("worker.stopped")


if __name__ == "__main__":
    asyncio.run(run())
