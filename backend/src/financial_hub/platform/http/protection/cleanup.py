"""Housekeeping for the worker process: purge expired idempotency keys and idle rate buckets.

Not scheduled here; the worker calls ``purge_protection_state`` periodically. An idle bucket is
deleted only after ``bucket_idle``: choose it at least ``capacity / refill_per_second`` of the
slowest policy, so a deleted bucket would have been full anyway (deleting refills it).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from financial_hub.platform.http.protection.idempotency import IdempotencyStore

DEFAULT_BUCKET_IDLE: Final = timedelta(hours=1)
_PURGE_BUCKETS_SQL: Final = text("DELETE FROM rate_limit_buckets WHERE updated_at < :before")


@dataclass(frozen=True, slots=True)
class CleanupResult:
    idempotency_records: int
    rate_limit_buckets: int


async def purge_protection_state(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
    bucket_idle: timedelta = DEFAULT_BUCKET_IDLE,
) -> CleanupResult:
    records = await IdempotencyStore(session_factory).purge_expired(now)
    async with session_factory() as session, session.begin():
        result = await session.execute(_PURGE_BUCKETS_SQL, {"before": now - bucket_idle})
    buckets = int(getattr(result, "rowcount", 0) or 0)
    return CleanupResult(idempotency_records=records, rate_limit_buckets=buckets)
