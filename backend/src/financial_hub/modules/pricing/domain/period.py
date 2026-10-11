"""Billing periods and day-based proration.

Proration rule (documented, integer-only): a partial period is charged
``amount x active_days / period_days`` where days are UTC calendar days of half-open ranges
``[start, end)`` and ``active_days`` is the overlap of the active range with the period. A day
counts as soon as any part of it is active (the active range is widened to whole UTC days). The
result is rounded once with ``ROUND_HALF_EVEN`` using ``divmod`` on integers: no ``Decimal``
division, no ``float``.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Self

from financial_hub.shared.errors import FieldError, ValidationFailed
from financial_hub.shared.money import Money


def _utc(value: object, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationFailed(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _date(value: object, name: str) -> date:
    # datetime is a date subclass; mixing them silently would drop the time of day.
    if type(value) is not date:
        raise TypeError(f"{name} must be date, got {type(value).__name__}")
    return value


@dataclass(frozen=True, slots=True)
class BillingPeriod:
    """Half-open period ``[start, end)`` in UTC."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", _utc(self.start, "start"))
        object.__setattr__(self, "end", _utc(self.end, "end"))
        if self.end <= self.start:
            raise ValidationFailed(
                "period end must be after start",
                errors=[FieldError("/period/end", "must be after start")],
            )

    def contains(self, instant: datetime) -> bool:
        return self.start <= _utc(instant, "instant") < self.end


def _ceil_day(instant: datetime) -> date:
    day = instant.date()
    return (
        day
        if instant == datetime(day.year, day.month, day.day, tzinfo=UTC)
        else (day + timedelta(days=1))
    )


@dataclass(frozen=True, slots=True)
class Proration:
    """Fraction ``active_days / period_days`` of a period, applied with integer arithmetic."""

    active_days: int
    period_days: int

    def __post_init__(self) -> None:
        for name in ("active_days", "period_days"):
            value: object = getattr(self, name)
            if type(value) is not int:
                raise TypeError(f"{name} must be int, got {type(value).__name__}")
        if self.period_days < 1:
            raise ValidationFailed("period_days must be >= 1")
        if not 0 <= self.active_days <= self.period_days:
            raise ValidationFailed("active_days must be between 0 and period_days")

    @classmethod
    def for_dates(
        cls, period_start: date, period_end: date, active_from: date, active_to: date | None
    ) -> Self:
        """Days of ``[active_from, active_to)`` inside ``[period_start, period_end)``."""
        start = _date(period_start, "period_start")
        end = _date(period_end, "period_end")
        if end <= start:
            raise ValidationFailed("period_end must be after period_start")
        lower = max(start, _date(active_from, "active_from"))
        upper = end if active_to is None else min(end, _date(active_to, "active_to"))
        return cls(max((upper - lower).days, 0), (end - start).days)

    @classmethod
    def for_period(
        cls, period: BillingPeriod, active_from: datetime, active_to: datetime | None
    ) -> Self:
        """UTC-day proration of ``[active_from, active_to)`` over ``period``."""
        return cls.for_dates(
            period.start.date(),
            _ceil_day(period.end),
            _utc(active_from, "active_from").date(),
            None if active_to is None else _ceil_day(_utc(active_to, "active_to")),
        )

    @property
    def is_full(self) -> bool:
        return self.active_days == self.period_days

    def apply(self, amount: Money) -> Money:
        """``amount x active_days / period_days``, rounded once half-even on integers."""
        numerator = abs(amount.minor_units) * self.active_days
        quotient, remainder = divmod(numerator, self.period_days)
        twice = 2 * remainder
        if twice > self.period_days or (twice == self.period_days and quotient % 2 == 1):
            quotient += 1
        sign = -1 if amount.minor_units < 0 else 1
        return Money(sign * quotient, amount.currency)

    def __str__(self) -> str:
        return f"{self.active_days}/{self.period_days}"
