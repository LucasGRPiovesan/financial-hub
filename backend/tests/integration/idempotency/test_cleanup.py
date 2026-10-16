"""``purge_protection_state``: expired idempotency keys and idle rate buckets go, live ones stay."""

import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import text

from financial_hub.platform.http.protection.cleanup import purge_protection_state
from financial_hub.platform.http.protection.idempotency import (
    Claim,
    IdempotencyStore,
    StoredResponse,
    request_hash,
)
from financial_hub.platform.http.protection.rate_limit import BucketPolicy, RateLimiter
from tests.integration.identity.support import Api

# Far enough in the future that every row other suites left behind is also stale: the counts
# below are lower bounds, the per-row assertions are exact.
FUTURE = datetime(2200, 1, 1, tzinfo=UTC)


async def _claim(api: Api, at: datetime, ttl: timedelta) -> Claim:
    claim = Claim(
        project_id=uuid4(), principal_id=f"opr_{secrets.token_hex(4)}", key="k", claimed_at=at
    )
    store = IdempotencyStore(api.factory)
    assert (
        await store.claim(
            claim,
            method="POST",
            path="/x",
            request_hash=request_hash("POST", "/x", b""),
            expires_at=at + ttl,
        )
        is None
    )
    await store.complete(claim, StoredResponse(status=201, headers={}, body=b"{}"))
    return claim


async def _exists(api: Api, sql: str, **params: object) -> bool:
    async with api.factory() as session:
        return bool(await session.scalar(text(f"SELECT EXISTS ({sql})"), params))


async def test_purge_removes_expired_keys_and_idle_buckets(api: Api) -> None:
    expired = await _claim(api, FUTURE - timedelta(days=2), timedelta(hours=24))
    live = await _claim(api, FUTURE - timedelta(hours=1), timedelta(hours=24))
    limiter = RateLimiter(api.factory)
    policy = BucketPolicy(capacity=5, refill_per_second=Decimal(1))
    idle_bucket, busy_bucket = f"test:{uuid4()}", f"test:{uuid4()}"
    await limiter.take(idle_bucket, policy, FUTURE - timedelta(hours=2))
    await limiter.take(busy_bucket, policy, FUTURE - timedelta(minutes=5))

    result = await purge_protection_state(api.factory, now=FUTURE, bucket_idle=timedelta(hours=1))

    assert result.idempotency_records >= 1
    assert result.rate_limit_buckets >= 1
    record = "SELECT 1 FROM idempotency_records WHERE principal_id = :principal"
    assert not await _exists(api, record, principal=expired.principal_id)
    assert await _exists(api, record, principal=live.principal_id)
    bucket = "SELECT 1 FROM rate_limit_buckets WHERE bucket_key = :key"
    assert not await _exists(api, bucket, key=idle_bucket)
    assert await _exists(api, bucket, key=busy_bucket)
