"""Worker process entrypoint. Dispatcher and scheduler loops are added in milestone M3."""

import asyncio
import signal
from pathlib import Path

import structlog

from financial_hub.platform.logging import configure_logging
from financial_hub.platform.settings import get_settings

HEARTBEAT_FILE = Path("/tmp/worker.heartbeat")  # noqa: S108 - container-local liveness marker
HEARTBEAT_INTERVAL_SECONDS = 15

logger = structlog.get_logger()


async def run() -> None:
    configure_logging(get_settings().log_level)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    logger.info("worker.started")
    while not stop.is_set():
        await asyncio.to_thread(HEARTBEAT_FILE.touch)
        try:
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_INTERVAL_SECONDS)
        except TimeoutError:
            continue
    logger.info("worker.stopped")


if __name__ == "__main__":
    asyncio.run(run())
