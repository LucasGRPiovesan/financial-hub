"""Realtime plumbing (ADR-0010): the process-wide ``LISTEN`` connection and the cross-project read.

``ListenWaker`` is the stream broker's wake-up source. Unlike ``PgListener`` (dispatcher) it backs
off exponentially between reconnect attempts, and it never blocks longer than the poll interval
while the connection is down, so the broker degrades to plain polling instead of stalling.

The cross-project read is the broker's own: a single global ``(txid, id)`` cursor below the
visibility horizon (ADR-0005), fanned out to subscribers in memory. Per-project reads (replay)
stay in ``repository``.
"""

import asyncio
import contextlib
import time
from collections.abc import Callable
from typing import Any, Final, Self
from uuid import UUID

import psycopg
import structlog
from psycopg import sql
from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession

from financial_hub.modules.events.domain import Event, EventCursor
from financial_hub.modules.events.infrastructure.pg import CHANNEL

logger = structlog.get_logger()

_COLUMNS: Final = """
    e.id, e.type, e.version, e.project_id, e.subject, e.occurred_at, e.correlation_id,
    e.causation_id, e.traceparent, e.data, e.txid::text AS txid
"""
_HORIZON: Final = "e.txid < pg_catalog.pg_snapshot_xmin(pg_catalog.pg_current_snapshot())"

_LATEST = text(
    f"SELECT e.txid::text AS txid, e.id FROM public.events e WHERE {_HORIZON} "  # noqa: S608
    "ORDER BY e.txid DESC, e.id DESC LIMIT 1"
)
_AFTER = text(
    f"SELECT {_COLUMNS} FROM public.events e "  # noqa: S608 - fixed fragments
    f"WHERE (e.txid, e.id) > (CAST(:txid AS xid8), :id) AND {_HORIZON} "
    "ORDER BY e.txid, e.id LIMIT :limit"
)


def _event(row: RowMapping) -> Event:
    return Event(
        id=row["id"],
        type=row["type"],
        version=row["version"],
        project_id=row["project_id"],
        subject=row["subject"],
        occurred_at=row["occurred_at"],
        correlation_id=row["correlation_id"],
        causation_id=row["causation_id"],
        traceparent=row["traceparent"],
        data=row["data"],
        txid=int(row["txid"]),
    )


async def latest_cursor(session: AsyncSession) -> EventCursor | None:
    """Position of the newest event below the horizon (``None`` for an empty log)."""
    row = (await session.execute(_LATEST)).mappings().one_or_none()
    return None if row is None else EventCursor(int(row["txid"]), row["id"])


async def events_after(
    session: AsyncSession, cursor: EventCursor | None, limit: int
) -> list[Event]:
    """Events of every project after ``cursor`` in ``(txid, id)`` order, below the horizon.

    ``None`` reads from the beginning of the log (zero ``txid`` and the nil UUID sort first).
    """
    start = cursor or EventCursor(0, UUID(int=0))
    params: dict[str, Any] = {"txid": str(start.txid), "id": start.id, "limit": limit}
    result = await session.execute(_AFTER, params)
    return [_event(row) for row in result.mappings()]


class ListenWaker:
    """``LISTEN`` on one connection. :meth:`wait` returns when something may have happened.

    ``True``: a notification arrived, or the connection was (re)established (notifications sent
    while it was down are lost, so the caller must catch up). ``False``: timeout, or the
    connection is down and being retried with exponential backoff.
    """

    def __init__(
        self,
        dsn: str,
        channel: str = CHANNEL,
        *,
        backoff_base_seconds: float = 0.5,
        backoff_cap_seconds: float = 30.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._dsn = dsn
        self._channel = channel
        self._base = backoff_base_seconds
        self._cap = backoff_cap_seconds
        self._monotonic = monotonic
        self._connection: psycopg.AsyncConnection[tuple[object, ...]] | None = None
        self._failures = 0
        self._retry_at = 0.0

    @property
    def connected(self) -> bool:
        return self._connection is not None and not self._connection.closed

    @property
    def backend_pid(self) -> int | None:
        """Postgres backend of the ``LISTEN`` connection (``None`` while disconnected)."""
        return self._connection.info.backend_pid if self.connected and self._connection else None

    def _fail(self, exc: BaseException) -> None:
        delay = min(self._cap, self._base * 2**self._failures)
        self._failures += 1
        self._retry_at = self._monotonic() + delay
        logger.warning("events.stream_listen_failed", error=str(exc), retry_in_seconds=delay)

    async def _connect(self) -> None:
        connection = await psycopg.AsyncConnection.connect(self._dsn, autocommit=True)
        try:
            await connection.execute(sql.SQL("LISTEN {}").format(sql.Identifier(self._channel)))
        except BaseException:
            await connection.close()
            raise
        self._connection = connection
        self._failures = 0
        logger.info("events.stream_listen_started", channel=self._channel)

    async def wait(self, seconds: float) -> bool:
        connection = self._connection
        if connection is None or connection.closed:
            if self._monotonic() < self._retry_at:
                await asyncio.sleep(seconds)
                return False
            try:
                await self._connect()
            except (psycopg.Error, OSError) as exc:
                self._fail(exc)
                await asyncio.sleep(seconds)
                return False
            return True
        try:
            async for _ in connection.notifies(timeout=seconds, stop_after=1):
                return True
            return False
        except psycopg.Error as exc:
            await self.close()
            self._fail(exc)
            return False

    async def close(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None and not connection.closed:
            with contextlib.suppress(psycopg.Error):
                await connection.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()
