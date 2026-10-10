from datetime import UTC, datetime, timedelta, timezone

import pytest

from financial_hub.shared.clock import Clock, FrozenClock, SystemClock

AT = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def test_system_clock_is_utc_aware() -> None:
    clock: Clock = SystemClock()
    before = datetime.now(UTC)

    now = clock.now()

    assert now.tzinfo is UTC
    assert before <= now <= datetime.now(UTC)


def test_frozen_clock_stands_still_until_moved() -> None:
    clock: Clock = FrozenClock(AT)

    assert clock.now() == clock.now() == AT


def test_frozen_clock_advance_and_set() -> None:
    clock = FrozenClock(AT)

    assert clock.advance(timedelta(minutes=5)) == AT + timedelta(minutes=5)
    assert clock.now() == AT + timedelta(minutes=5)
    clock.set(AT - timedelta(days=1))
    assert clock.now() == AT - timedelta(days=1)


def test_frozen_clock_normalizes_offsets_to_utc() -> None:
    local = datetime(2026, 10, 10, 9, 0, tzinfo=timezone(timedelta(hours=-3)))

    now = FrozenClock(local).now()

    assert now == AT
    assert now.tzinfo is UTC


def test_frozen_clock_rejects_naive_datetimes() -> None:
    naive = datetime(2026, 10, 10, 12, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        FrozenClock(naive)
    clock = FrozenClock(AT)
    with pytest.raises(ValueError, match="timezone-aware"):
        clock.set(naive)
