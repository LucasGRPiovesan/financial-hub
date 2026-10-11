"""Postgres-only messaging plumbing (ADR-0005): NOTIFY wake-ups, LISTEN, leader advisory lock.

``LISTEN`` and session advisory locks need one long-lived connection each, outside the pooled
sessions, so they use a raw psycopg autocommit connection built from the same database URL.
"""

import asyncio
import contextlib
from typing import Final, Self

import psycopg
import structlog
from psycopg import sql
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncEngine

CHANNEL: Final = "fh_events"
"""NOTIFY channel; payload is empty: a notification only means "there may be work"."""

logger = structlog.get_logger()


def libpq_dsn(database_url: str) -> str:
    """``postgresql+psycopg://...`` (SQLAlchemy) to a plain libpq URL for ``psycopg.connect``."""
    return make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)


def engine_dsn(engine: AsyncEngine) -> str:
    return libpq_dsn(engine.url.render_as_string(hide_password=False))


class PgNotifier:
    """Sends the wake-up NOTIFY on its own connection, after the business transaction commits."""

    def __init__(self, engine: AsyncEngine, channel: str = CHANNEL) -> None:
        self._engine = engine
        self._channel = channel

    async def notify(self) -> None:
        async with self._engine.connect() as connection:
            await connection.execute(
                text("SELECT pg_catalog.pg_notify(:channel, '')"), {"channel": self._channel}
            )
            await connection.commit()


class PgListener:
    """``LISTEN`` with reconnects. :meth:`wait` returns on a notification or after ``timeout``.

    Missing a notification is harmless: the dispatcher also polls, so a broken connection only
    degrades latency to the polling interval.
    """

    def __init__(self, dsn: str, channel: str = CHANNEL) -> None:
        self._dsn = dsn
        self._channel = channel
        self._connection: psycopg.AsyncConnection[tuple[object, ...]] | None = None

    async def _connect(self) -> psycopg.AsyncConnection[tuple[object, ...]]:
        if self._connection is None or self._connection.closed:
            connection = await psycopg.AsyncConnection.connect(self._dsn, autocommit=True)
            await connection.execute(sql.SQL("LISTEN {}").format(sql.Identifier(self._channel)))
            self._connection = connection
            logger.info("events.listen_started", channel=self._channel)
        return self._connection

    async def wait(self, seconds: float) -> bool:
        """``True`` when woken by a notification, ``False`` on timeout or connection trouble."""
        try:
            connection = await self._connect()
            async for _ in connection.notifies(timeout=seconds, stop_after=1):
                return True
            return False
        except psycopg.Error as exc:
            logger.warning("events.listen_failed", error=str(exc))
            await self.close()
            await asyncio.sleep(seconds)
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


class AdvisoryLeaderLock:
    """Session-level ``pg_try_advisory_lock`` on a dedicated connection (scheduler leadership).

    Leadership lasts as long as the connection: if it dies, Postgres releases the lock and
    another process can take over. :meth:`held` re-checks the connection on every tick.
    """

    def __init__(self, dsn: str, lock_class: int, key: int) -> None:
        self._dsn = dsn
        self._class = lock_class
        self._key = key
        self._connection: psycopg.AsyncConnection[tuple[object, ...]] | None = None

    @property
    def is_leader(self) -> bool:
        return self._connection is not None and not self._connection.closed

    async def try_acquire(self) -> bool:
        if self.is_leader:
            return True
        connection = await psycopg.AsyncConnection.connect(self._dsn, autocommit=True)
        try:
            cursor = await connection.execute(
                "SELECT pg_catalog.pg_try_advisory_lock(%s, %s)", (self._class, self._key)
            )
            row = await cursor.fetchone()
        except BaseException:
            await connection.close()
            raise
        if row is None or row[0] is not True:
            await connection.close()
            return False
        self._connection = connection
        return True

    async def held(self) -> bool:
        """Whether the lock is still ours (the connection answers); drops it otherwise."""
        connection = self._connection
        if connection is None or connection.closed:
            self._connection = None
            return False
        try:
            await connection.execute("SELECT 1")
        except psycopg.Error as exc:
            logger.warning("scheduler.leadership_lost", error=str(exc))
            await self.release()
            return False
        return True

    async def release(self) -> None:
        connection, self._connection = self._connection, None
        if connection is None or connection.closed:
            return
        try:
            await connection.execute(
                "SELECT pg_catalog.pg_advisory_unlock(%s, %s)", (self._class, self._key)
            )
        except psycopg.Error:  # pragma: no cover - the lock dies with the connection anyway
            pass
        finally:
            await connection.close()
