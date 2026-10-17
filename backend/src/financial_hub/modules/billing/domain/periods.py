"""Monthly billing periods on an anchor day (docs/DOMAIN.md "Billing").

A subscription bills ``[start, end)`` periods whose boundaries are **UTC midnights** on its
``anchor_day`` of each month. In a month shorter than the anchor day the boundary is the month's
last day (anchor 31: Jan 31, Feb 28/29, Mar 31, ...), so the cycle never drifts.

- The first period runs from the billing start (the trial end, or the start) to the next boundary;
  when the start is not on a boundary it is shorter than a month and fixed fees are prorated
  against :func:`full_period` (the whole anchor period containing it).
- Every later period runs from one boundary to the next.
"""

from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, datetime

MIN_ANCHOR_DAY = 1
MAX_ANCHOR_DAY = 31


@dataclass(frozen=True, slots=True)
class Period:
    """Half-open ``[start, end)`` of timezone-aware instants."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        for name in ("start", "end"):
            value: datetime = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"period {name} must be timezone-aware")
        if self.end <= self.start:
            raise ValueError("period end must be after its start")


def _check_anchor(anchor_day: int) -> None:
    if type(anchor_day) is not int or not MIN_ANCHOR_DAY <= anchor_day <= MAX_ANCHOR_DAY:
        raise ValueError(f"anchor_day must be an int in {MIN_ANCHOR_DAY}..{MAX_ANCHOR_DAY}")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


def _shift(year: int, month: int, months: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + months
    return index // 12, index % 12 + 1


def boundary(year: int, month: int, anchor_day: int) -> datetime:
    """The period boundary of ``year``/``month``: the anchor day, clamped to the month length."""
    _check_anchor(anchor_day)
    return datetime(year, month, min(anchor_day, monthrange(year, month)[1]), tzinfo=UTC)


def next_boundary(after: datetime, anchor_day: int) -> datetime:
    """The first boundary strictly after ``after``."""
    at = _utc(after)
    candidate = boundary(at.year, at.month, anchor_day)
    if candidate <= at:
        candidate = boundary(*_shift(at.year, at.month, 1), anchor_day)
    return candidate


def previous_boundary(at: datetime, anchor_day: int) -> datetime:
    """The last boundary at or before ``at``."""
    moment = _utc(at)
    candidate = boundary(moment.year, moment.month, anchor_day)
    if candidate > moment:
        candidate = boundary(*_shift(moment.year, moment.month, -1), anchor_day)
    return candidate


def full_period(at: datetime, anchor_day: int) -> Period:
    """The whole anchor period containing ``at`` (the proration denominator)."""
    return Period(previous_boundary(at, anchor_day), next_boundary(at, anchor_day))


def period_from(start: datetime, anchor_day: int) -> Period:
    """The period that starts at ``start`` and ends on the next boundary."""
    return Period(_utc(start), next_boundary(start, anchor_day))


def anchor_day_of(at: datetime) -> int:
    """The default anchor: the UTC day of the billing start."""
    return _utc(at).day
