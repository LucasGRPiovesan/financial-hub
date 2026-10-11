from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from financial_hub.modules.pricing.domain import BillingPeriod, Proration
from financial_hub.shared.errors import ValidationFailed
from financial_hub.shared.money import BRL, Money

SEP = BillingPeriod(datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC))


def test_billing_period_is_half_open_utc() -> None:
    brt = timezone(timedelta(hours=-3))
    period = BillingPeriod(datetime(2026, 8, 31, 21, tzinfo=brt), datetime(2026, 10, 1, tzinfo=UTC))
    assert period == SEP
    assert period.start.tzinfo is UTC
    assert SEP.contains(datetime(2026, 9, 1, tzinfo=UTC))
    assert not SEP.contains(datetime(2026, 10, 1, tzinfo=UTC))
    with pytest.raises(ValidationFailed, match="after start"):
        BillingPeriod(SEP.end, SEP.start)
    with pytest.raises(ValidationFailed, match="timezone-aware"):
        BillingPeriod(datetime(2026, 9, 1), SEP.end)
    with pytest.raises(TypeError):
        BillingPeriod(date(2026, 9, 1), SEP.end)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    ("active_from", "active_to", "days"),
    [
        (date(2026, 9, 1), None, 30),  # whole period
        (date(2026, 8, 1), date(2026, 12, 1), 30),  # active range wider than the period
        (date(2026, 9, 21), None, 10),  # starts on the 21st: 21..30 = 10 days
        (date(2026, 9, 1), date(2026, 9, 8), 7),  # ends (exclusive) on the 8th
        (date(2026, 9, 10), date(2026, 9, 10), 0),  # empty range
        (date(2026, 10, 5), None, 0),  # after the period
        (date(2026, 7, 1), date(2026, 8, 1), 0),  # before the period
    ],
)
def test_proration_for_dates(active_from: date, active_to: date | None, days: int) -> None:
    proration = Proration.for_dates(date(2026, 9, 1), date(2026, 10, 1), active_from, active_to)
    assert proration == Proration(days, 30)


def test_proration_for_period_counts_partial_days_as_whole_utc_days() -> None:
    # active from 2026-09-21T15:00Z -> the 21st counts; until 2026-09-25T00:00:01Z -> the 25th too
    proration = Proration.for_period(
        SEP,
        datetime(2026, 9, 21, 15, tzinfo=UTC),
        datetime(2026, 9, 25, 0, 0, 1, tzinfo=UTC),
    )
    assert proration == Proration(5, 30)
    assert Proration.for_period(SEP, SEP.start, None).is_full
    february = BillingPeriod(
        datetime(2026, 2, 1, tzinfo=UTC), datetime(2026, 2, 15, 12, tzinfo=UTC)
    )
    assert Proration.for_period(february, february.start, None) == Proration(15, 15)


@pytest.mark.parametrize(
    ("units", "active", "period", "expected"),
    [
        (9990, 10, 30, 3330),  # exact
        (9990, 1, 31, 322),  # 322.258 -> 322
        (1000, 1, 3, 333),  # 333.33 -> 333
        (2000, 1, 3, 667),  # 666.67 -> 667
        (1, 1, 2, 0),  # 0.5 -> half-even -> 0
        (3, 1, 2, 2),  # 1.5 -> half-even -> 2
        (5, 1, 2, 2),  # 2.5 -> half-even -> 2
        (-3, 1, 2, -2),  # symmetric for negatives
        (9990, 0, 30, 0),
        (9990, 30, 30, 9990),
    ],
)
def test_proration_apply_half_even_on_integers(
    units: int, active: int, period: int, expected: int
) -> None:
    assert Proration(active, period).apply(Money(units, BRL)) == Money(expected, BRL)


def test_proration_validation() -> None:
    with pytest.raises(ValidationFailed, match="period_days"):
        Proration(0, 0)
    with pytest.raises(ValidationFailed, match="active_days"):
        Proration(31, 30)
    with pytest.raises(ValidationFailed, match="active_days"):
        Proration(-1, 30)
    with pytest.raises(TypeError):
        Proration(True, 30)
    with pytest.raises(ValidationFailed, match="after period_start"):
        Proration.for_dates(date(2026, 10, 1), date(2026, 9, 1), date(2026, 9, 1), None)
    with pytest.raises(TypeError):
        Proration.for_dates(
            datetime(2026, 9, 1, tzinfo=UTC), date(2026, 10, 1), date(2026, 9, 1), None
        )
    assert str(Proration(3, 30)) == "3/30"
