"""``RateLimiter.take``: one atomic upsert per check, exact refill, against real Postgres."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from financial_hub.platform.http.protection.rate_limit import BucketPolicy, RateLimiter
from tests.integration.identity.support import Api

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def bucket() -> str:
    return f"test:{uuid4()}"


async def test_concurrent_takes_never_overspend_a_bucket(api: Api) -> None:
    limiter = RateLimiter(api.factory)
    policy = BucketPolicy(capacity=5, refill_per_second=Decimal("0.0001"))
    for _ in range(3):
        key = bucket()
        decisions = await asyncio.gather(*(limiter.take(key, policy, NOW) for _ in range(25)))

        assert sum(decision.allowed for decision in decisions) == 5
        assert sorted(d.remaining for d in decisions if d.allowed) == [0, 1, 2, 3, 4]
        assert all(d.remaining == 0 for d in decisions if not d.allowed)


async def test_tokens_refill_with_elapsed_time(api: Api) -> None:
    limiter = RateLimiter(api.factory)
    policy = BucketPolicy(capacity=2, refill_per_second=Decimal("0.5"))
    key = bucket()

    first = await limiter.take(key, policy, NOW)
    second = await limiter.take(key, policy, NOW)
    denied = await limiter.take(key, policy, NOW)
    assert (first.allowed, first.remaining) == (True, 1)
    assert (second.allowed, second.remaining) == (True, 0)
    assert not denied.allowed
    assert denied.retry_after_seconds == 2  # one token at 0.5/s
    assert denied.headers()["Retry-After"] == "2"

    assert not (await limiter.take(key, policy, NOW + timedelta(seconds=1))).allowed
    assert (await limiter.take(key, policy, NOW + timedelta(seconds=2))).allowed
    # Never more than the capacity, however long the bucket idled.
    later = NOW + timedelta(days=1)
    assert [(await limiter.take(key, policy, later)).allowed for _ in range(3)] == [
        True,
        True,
        False,
    ]


async def test_fractional_progress_is_kept(api: Api) -> None:
    limiter = RateLimiter(api.factory)
    policy = BucketPolicy(capacity=1, refill_per_second=Decimal("0.4"))
    key = bucket()

    assert (await limiter.take(key, policy, NOW)).allowed
    half = await limiter.take(key, policy, NOW + timedelta(milliseconds=1250))  # 0.5 token
    assert not half.allowed
    assert half.retry_after_seconds == 2  # ceil(0.5 / 0.4)
    assert (await limiter.take(key, policy, NOW + timedelta(milliseconds=2500))).allowed


async def test_a_clock_going_backwards_mints_no_tokens(api: Api) -> None:
    limiter = RateLimiter(api.factory)
    policy = BucketPolicy(capacity=1, refill_per_second=Decimal(1))
    key = bucket()

    assert (await limiter.take(key, policy, NOW)).allowed
    assert not (await limiter.take(key, policy, NOW - timedelta(hours=1))).allowed
    # The bucket kept its newest timestamp: the skewed check did not reset the refill origin.
    assert not (await limiter.take(key, policy, NOW + timedelta(milliseconds=500))).allowed
    assert (await limiter.take(key, policy, NOW + timedelta(seconds=1))).allowed
