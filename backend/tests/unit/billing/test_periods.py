"""Monthly periods on an anchor day: UTC-midnight boundaries clamped to short months."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from financial_hub.modules.billing.domain import (
    Period,
    anchor_day_of,
    boundary,
    full_period,
    next_boundary,
    period_from,
    previous_boundary,
)


def utc(year: int, month: int, day: int, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


@pytest.mark.parametrize(
    ("year", "month", "anchor", "expected"),
    [
        (2026, 3, 1, utc(2026, 3, 1)),
        (2026, 3, 31, utc(2026, 3, 31)),
        (2026, 2, 31, utc(2026, 2, 28)),  # clamped to the month's last day
        (2028, 2, 30, utc(2028, 2, 29)),  # leap year
        (2026, 4, 31, utc(2026, 4, 30)),
    ],
)
def test_boundary_clamps_the_anchor_to_the_month(
    year: int, month: int, anchor: int, expected: datetime
) -> None:
    assert boundary(year, month, anchor) == expected


def test_anchor_31_never_drifts_after_a_short_month() -> None:
    at = utc(2026, 1, 31)
    seen: list[datetime] = []
    for _ in range(4):
        at = next_boundary(at, 31)
        seen.append(at)
    assert seen == [utc(2026, 2, 28), utc(2026, 3, 31), utc(2026, 4, 30), utc(2026, 5, 31)]


def test_next_boundary_is_strictly_after_and_wraps_the_year() -> None:
    assert next_boundary(utc(2026, 3, 1), 1) == utc(2026, 4, 1)
    assert next_boundary(utc(2026, 2, 28, 23), 1) == utc(2026, 3, 1)
    assert next_boundary(utc(2026, 12, 15), 1) == utc(2027, 1, 1)


def test_previous_boundary_is_at_or_before() -> None:
    assert previous_boundary(utc(2026, 3, 1), 1) == utc(2026, 3, 1)
    assert previous_boundary(utc(2026, 3, 1) - timedelta(microseconds=1), 1) == utc(2026, 2, 1)
    assert previous_boundary(utc(2026, 1, 10), 15) == utc(2025, 12, 15)


def test_period_from_a_boundary_is_a_whole_period() -> None:
    period = period_from(utc(2026, 3, 1), 1)
    assert period == Period(utc(2026, 3, 1), utc(2026, 4, 1))
    assert full_period(period.start, 1) == period


def test_first_period_from_mid_cycle_is_short_and_full_period_is_the_denominator() -> None:
    period = period_from(utc(2026, 3, 11), 1)
    assert period == Period(utc(2026, 3, 11), utc(2026, 4, 1))
    assert full_period(period.start, 1) == Period(utc(2026, 3, 1), utc(2026, 4, 1))


def test_inputs_in_other_offsets_are_normalized_to_utc() -> None:
    brt = timezone(timedelta(hours=-3))
    late_evening = datetime(2026, 3, 31, 22, tzinfo=brt)  # 2026-04-01 01:00 UTC
    assert next_boundary(late_evening, 1) == utc(2026, 5, 1)
    assert anchor_day_of(late_evening) == 1
    assert period_from(late_evening, 1).start.tzinfo is UTC


@pytest.mark.parametrize("anchor", [0, 32, -1, True])
def test_anchor_day_out_of_range_is_rejected(anchor: int) -> None:
    with pytest.raises(ValueError, match="anchor_day"):
        boundary(2026, 3, anchor)


def test_naive_datetimes_are_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        next_boundary(datetime(2026, 3, 1), 1)
    with pytest.raises(ValueError, match="timezone-aware"):
        Period(datetime(2026, 3, 1), utc(2026, 4, 1))


def test_period_must_not_be_empty() -> None:
    with pytest.raises(ValueError, match="after its start"):
        Period(utc(2026, 3, 1), utc(2026, 3, 1))
