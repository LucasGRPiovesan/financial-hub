"""Invoice math golden examples: every expected amount is computed by hand in the comments.

The draft invoice rates each plan price over the period with the period's proration (fixed
amounts only) and sums the integer line totals (``BillingService._draft``); these tests compose
the same pure pieces.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from financial_hub.modules.billing.domain import (
    InvoiceStatus,
    Period,
    can_move,
    full_period,
    invoice_amounts,
    period_from,
)
from financial_hub.modules.pricing.domain import (
    BillingPeriod,
    FlatFee,
    Graduated,
    PerUnit,
    PriceModel,
    Proration,
    Tier,
    price_lines,
)
from financial_hub.shared.money import BRL, Money

MONTHLY_FEE = FlatFee(BRL, Money(4990, BRL))  # BRL 49.90
API_CALLS = PerUnit(BRL, Decimal("0.05"))
# First 1000 free; then BRL 0.01 each plus a one-off BRL 10.00 when the tier is entered.
STORAGE = Graduated(
    BRL,
    (
        Tier(Decimal(1000), Decimal(0)),
        Tier(None, Decimal("0.01"), Money(1000, BRL)),
    ),
)


def utc(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=UTC)


def proration_of(period: Period, anchor_day: int) -> Proration | None:
    """The same rule as the service: the share of the anchor period, ``None`` when whole."""
    full = full_period(period.start, anchor_day)
    proration = Proration.for_period(BillingPeriod(full.start, full.end), period.start, period.end)
    return None if proration.is_full else proration


def line_total(model: PriceModel, usage: Decimal | int, proration: Proration | None) -> int:
    return sum(line.amount.minor_units for line in price_lines(model, usage, proration=proration))


def invoice_lines(period: Period, anchor_day: int, calls: int, storage: int) -> list[int]:
    proration = proration_of(period, anchor_day)
    return [
        line_total(MONTHLY_FEE, 1, proration),
        line_total(API_CALLS, calls, proration),
        line_total(STORAGE, storage, proration),
    ]


def test_full_period_flat_plus_per_unit_plus_graduated() -> None:
    period = period_from(utc(2026, 3, 1), 1)  # [Mar 1, Apr 1): a whole period, no proration
    assert proration_of(period, 1) is None
    lines = invoice_lines(period, 1, calls=1500, storage=1500)
    # fee 4990; calls 1500 x 0.05 = 75.00 -> 7500; storage 0 + 500 x 0.01 + 10.00 -> 1500
    assert lines == [4990, 7500, 1500]
    amounts = invoice_amounts(lines)
    assert (amounts.total, amounts.credits_applied, amounts.amount_due) == (13990, 0, 13990)


def test_mid_cycle_start_prorates_fixed_amounts_only() -> None:
    period = period_from(utc(2026, 3, 11), 1)  # [Mar 11, Apr 1) inside [Mar 1, Apr 1)
    proration = proration_of(period, 1)
    assert proration == Proration(21, 31)
    lines = invoice_lines(period, 1, calls=1500, storage=1500)
    # fee 4990 x 21 / 31 = 3380.32 -> 3380 (half-even, integers)
    # calls are per unit: never prorated -> 7500
    # storage is usage too: its tier flat fee is charged by reaching the tier, not by time -> 1500
    assert lines == [3380, 7500, 1500]
    assert invoice_amounts(lines).amount_due == 12380


def test_short_month_anchor_31_prorates_against_the_clamped_period() -> None:
    period = period_from(utc(2026, 2, 14), 31)  # [Feb 14, Feb 28) inside [Jan 31, Feb 28)
    assert period.end == utc(2026, 2, 28)
    assert proration_of(period, 31) == Proration(14, 28)
    # fee 4990 x 14 / 28 = 2495 exactly; no usage
    assert invoice_lines(period, 31, calls=0, storage=0) == [2495, 0, 0]


def test_proration_rounds_half_even() -> None:
    # 4990 x 1 / 4 = 1247.5 -> 1248 (even); 4990 x 3 / 4 = 3742.5 -> 3742 (even)
    assert line_total(MONTHLY_FEE, 1, Proration(1, 4)) == 1248
    assert line_total(MONTHLY_FEE, 1, Proration(3, 4)) == 3742


def test_zero_usage_on_a_usage_only_plan_is_a_zero_invoice() -> None:
    period = period_from(utc(2026, 3, 1), 1)
    lines = [line_total(API_CALLS, 0, proration_of(period, 1))]
    assert invoice_amounts(lines).amount_due == 0


def test_invoice_amounts_are_integer_minor_units() -> None:
    assert invoice_amounts([]).total == 0
    with pytest.raises(TypeError, match="int minor units"):
        invoice_amounts([Decimal("1.5")])  # pyright: ignore[reportArgumentType]
    with pytest.raises(TypeError, match="int minor units"):
        invoice_amounts([1.5])  # pyright: ignore[reportArgumentType]
    with pytest.raises(TypeError, match="int minor units"):
        invoice_amounts([True])
    with pytest.raises(ValueError, match=">= 0"):
        invoice_amounts([100, -1])


def test_credits_reduce_the_amount_due_but_never_below_zero() -> None:
    amounts = invoice_amounts([4990, 7500], credits_applied=2490)
    assert (amounts.total, amounts.credits_applied, amounts.amount_due) == (12490, 2490, 10000)
    assert invoice_amounts([100], credits_applied=100).amount_due == 0
    with pytest.raises(ValueError, match="cannot exceed"):
        invoice_amounts([100], credits_applied=101)


@pytest.mark.parametrize(
    ("current", "target", "allowed"),
    [
        (InvoiceStatus.DRAFT, InvoiceStatus.FINALIZED, True),
        (InvoiceStatus.DRAFT, InvoiceStatus.VOID, True),
        (InvoiceStatus.DRAFT, InvoiceStatus.PAID, False),
        (InvoiceStatus.FINALIZED, InvoiceStatus.PAID, True),
        (InvoiceStatus.FINALIZED, InvoiceStatus.VOID, True),
        (InvoiceStatus.FINALIZED, InvoiceStatus.UNCOLLECTIBLE, True),
        (InvoiceStatus.FINALIZED, InvoiceStatus.DRAFT, False),
        (InvoiceStatus.UNCOLLECTIBLE, InvoiceStatus.PAID, True),
        (InvoiceStatus.PAID, InvoiceStatus.VOID, False),
        (InvoiceStatus.PAID, InvoiceStatus.FINALIZED, False),
        (InvoiceStatus.VOID, InvoiceStatus.PAID, False),
        (InvoiceStatus.VOID, InvoiceStatus.FINALIZED, False),
    ],
)
def test_invoice_lifecycle_moves(
    current: InvoiceStatus, target: InvoiceStatus, allowed: bool
) -> None:
    assert can_move(current, target) is allowed
