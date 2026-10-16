"""Webhook retry schedule: ~30 s .. 24 h with +-20% jitter, 8 attempts, then dead."""

from datetime import timedelta

import pytest

from financial_hub.modules.webhooks.domain import RetrySchedule

SCHEDULE = RetrySchedule()
BASE = [30, 120, 600, 1800, 7200, 28800, 86400]


def test_eight_attempts_then_dead() -> None:
    assert SCHEDULE.max_attempts == 8
    assert not SCHEDULE.exhausted(7)
    assert SCHEDULE.exhausted(8)


@pytest.mark.parametrize(("attempt", "base"), list(enumerate(BASE, start=1)))
def test_delay_without_jitter_is_the_base(attempt: int, base: int) -> None:
    assert SCHEDULE.delay(attempt, lambda: 0.5) == timedelta(seconds=base)


@pytest.mark.parametrize(("attempt", "base"), list(enumerate(BASE, start=1)))
def test_jitter_stays_within_twenty_percent(attempt: int, base: int) -> None:
    low = SCHEDULE.delay(attempt, lambda: 0.0)
    high = SCHEDULE.delay(attempt, lambda: 0.999999)

    assert low == timedelta(seconds=base * 0.8)
    assert timedelta(seconds=base * 1.19) < high <= timedelta(seconds=base * 1.2)


def test_schedule_is_monotonic_and_spans_about_35_hours() -> None:
    delays = [SCHEDULE.delay(n, lambda: 0.5) for n in range(1, SCHEDULE.max_attempts)]

    assert delays == sorted(delays)
    assert delays[0] == timedelta(seconds=30) and delays[-1] == timedelta(hours=24)
    assert timedelta(hours=34) < sum(delays, timedelta()) < timedelta(hours=36)


def test_out_of_range_attempts_and_draws_are_clamped() -> None:
    assert SCHEDULE.delay(0, lambda: 0.5) == timedelta(seconds=30)
    assert SCHEDULE.delay(99, lambda: 0.5) == timedelta(hours=24)
    assert SCHEDULE.delay(1, lambda: 5.0) == timedelta(seconds=36)
