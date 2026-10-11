"""Golden tests: every expected line and total below is computed by hand in the comments."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from financial_hub.modules.pricing.domain import (
    ROUNDING_POLICY,
    AggregatedUsage,
    BaseFeeWithOverage,
    BillingPeriod,
    ChargeLine,
    FlatFee,
    Graduated,
    MinimumCommitment,
    Package,
    PackageRounding,
    Percentage,
    PerUnit,
    PriceModel,
    PriceVersion,
    Proration,
    RatedCharge,
    Tier,
    Volume,
    price_lines,
    rate,
)
from financial_hub.shared.errors import ValidationFailed
from financial_hub.shared.money import BRL, JPY, USD, Money

OCT = BillingPeriod(datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC))


def brl(units: int) -> Money:
    return Money(units, BRL)


def amounts(lines: tuple[ChargeLine, ...]) -> list[int]:
    return [line.amount.minor_units for line in lines]


def total(lines: tuple[ChargeLine, ...]) -> int:
    return sum(amounts(lines))


def version(model: PriceModel, **overrides: object) -> PriceVersion:
    values: dict[str, object] = {
        "id": uuid4(),
        "price_id": uuid4(),
        "version": 1,
        "model": model,
        "effective_from": datetime(2026, 1, 1, tzinfo=UTC),
    }
    values.update(overrides)
    return PriceVersion(**values)  # pyright: ignore[reportArgumentType]


# --- per unit -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("unit_price", "quantity", "expected"),
    [
        ("0.25", 37, 925),  # 37 x 0.25 = 9.25 BRL
        ("0.0015", 1_234_567, 185185),  # 1851.8505 BRL = 185185.05 cents -> 185185
        ("0.005", 1, 0),  # 0.5 cent -> half-even -> 0
        ("0.005", 3, 2),  # 1.5 cents -> half-even -> 2
        ("0.025", 1, 2),  # 2.5 cents -> half-even -> 2 (half-up would give 3)
        ("0.035", 1, 4),  # 3.5 cents -> half-even -> 4
        ("1.99", 0, 0),  # zero usage
    ],
)
def test_per_unit_golden(unit_price: str, quantity: int, expected: int) -> None:
    lines = price_lines(PerUnit(BRL, Decimal(unit_price)), quantity)
    assert len(lines) == 1
    assert lines[0].key == "pricing.per_unit"
    assert lines[0].quantity == quantity
    assert lines[0].unit_amount == Decimal(unit_price)
    assert lines[0].amount == brl(expected)


def test_per_unit_fractional_quantity_and_zero_exponent_currency() -> None:
    # 2.5 hours x 40.00 = 100.00
    assert amounts(price_lines(PerUnit(BRL, Decimal(40)), Decimal("2.5"))) == [10000]
    # 3 x 1.5 JPY = 4.5 -> half-even -> 4 JPY
    assert price_lines(PerUnit(JPY, Decimal("1.5")), 3)[0].amount == Money(4, JPY)


# --- package --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rounding", "quantity", "packages", "expected"),
    [
        (PackageRounding.UP, 2500, 3, 1500),  # ceil(2500/1000) = 3 x 5.00
        (PackageRounding.DOWN, 2500, 2, 1000),  # floor(2500/1000) = 2 x 5.00
        (PackageRounding.UP, 2000, 2, 1000),  # exactly on a block boundary
        (PackageRounding.DOWN, 2000, 2, 1000),
        (PackageRounding.UP, 1, 1, 500),
        (PackageRounding.DOWN, 999, 0, 0),
        (PackageRounding.UP, 0, 0, 0),
        (PackageRounding.UP, Decimal("1000.5"), 2, 1000),
    ],
)
def test_package_golden(
    rounding: PackageRounding, quantity: int | Decimal, packages: int, expected: int
) -> None:
    model = Package(BRL, 1000, brl(500), rounding)
    (line,) = price_lines(model, quantity)
    assert line.key == "pricing.package"
    assert line.quantity == packages
    assert line.unit_amount == Decimal("5.00")
    assert line.amount == brl(expected)
    assert line.metadata == {
        "units": str(quantity),
        "package_size": "1000",
        "rounding": rounding.value,
    }


# --- graduated ------------------------------------------------------------------------------

TIERS = (
    Tier(Decimal(100), Decimal("0.10")),
    Tier(Decimal(1000), Decimal("0.08"), flat_fee=brl(100)),
    Tier(None, Decimal("0.05")),
)


def test_graduated_golden_across_all_tiers() -> None:
    lines = price_lines(Graduated(BRL, TIERS), 1500)
    # tier 1: 100 x 0.10 = 10.00; tier 2: 900 x 0.08 = 72.00 + flat 1.00; tier 3: 500 x 0.05
    assert [(line.key, line.quantity, line.amount.minor_units) for line in lines] == [
        ("pricing.graduated.tier", 100, 1000),
        ("pricing.graduated.tier", 900, 7200),
        ("pricing.graduated.tier_flat_fee", 1, 100),
        ("pricing.graduated.tier", 500, 2500),
    ]
    assert total(lines) == 10800
    assert lines[0].metadata == {"tier": "1", "from": "0", "up_to": "100"}
    assert lines[3].metadata == {"tier": "3", "from": "1000", "up_to": "inf"}


def test_graduated_exactly_at_a_bound_stays_in_the_lower_tier() -> None:
    lines = price_lines(Graduated(BRL, TIERS), 100)
    assert amounts(lines) == [1000]
    lines = price_lines(Graduated(BRL, TIERS), 1000)
    assert amounts(lines) == [1000, 7200, 100]  # no tier-3 line at exactly 1000


def test_graduated_one_unit_past_a_bound_enters_the_next_tier_with_its_flat_fee() -> None:
    lines = price_lines(Graduated(BRL, TIERS), 101)
    assert amounts(lines) == [1000, 8, 100]  # 1 x 0.08 = 0.08
    assert total(lines) == 1108


def test_graduated_zero_usage_is_one_zero_line() -> None:
    (line,) = price_lines(Graduated(BRL, TIERS), 0)
    assert line.quantity == 0
    assert line.amount == brl(0)


def test_graduated_rounds_each_line_once() -> None:
    tiers = (Tier(Decimal(1), Decimal("0.005")), Tier(None, Decimal("0.005")))
    # tier 1: 0.5 cent -> 0; tier 2: 2 x 0.005 = 1 cent -> 1 (line-level, not on the total)
    assert amounts(price_lines(Graduated(BRL, tiers), 3)) == [0, 1]


# --- volume ---------------------------------------------------------------------------------

VOLUME_TIERS = (
    Tier(Decimal(100), Decimal("0.10")),
    Tier(Decimal(1000), Decimal("0.08")),
    Tier(None, Decimal("0.05"), flat_fee=brl(250)),
)


@pytest.mark.parametrize(
    ("quantity", "expected", "tier"),
    [
        (0, [0], "1"),
        (100, [1000], "1"),  # 100 x 0.10, exactly on the bound
        (101, [808], "2"),  # 101 x 0.08 = 8.08: the volume cliff, cheaper than 100 units
        (1000, [8000], "2"),
        (1500, [7500, 250], "3"),  # 1500 x 0.05 = 75.00 + flat fee 2.50
    ],
)
def test_volume_golden(quantity: int, expected: list[int], tier: str) -> None:
    lines = price_lines(Volume(BRL, VOLUME_TIERS), quantity)
    assert amounts(lines) == expected
    assert lines[0].key == "pricing.volume"
    assert lines[0].quantity == quantity
    assert lines[0].metadata["tier"] == tier


# --- percentage -----------------------------------------------------------------------------

CARD_FEE = Percentage(BRL, 299, minimum=brl(50), maximum=brl(1000))


@pytest.mark.parametrize(
    ("base", "expected", "cap"),
    [
        (10000, 299, None),  # 2.99% of 100.00 = 2.99
        (1000, 50, "minimum"),  # 0.299 -> 30 cents -> raised to the 0.50 minimum
        (100000, 1000, "maximum"),  # 29.90 -> capped at 10.00
        (0, 0, None),  # nothing transacted, no minimum
    ],
)
def test_percentage_golden(base: int, expected: int, cap: str | None) -> None:
    (line,) = price_lines(CARD_FEE, brl(base))
    assert line.key == "pricing.percentage"
    assert line.quantity == brl(base).to_decimal()
    assert line.unit_amount == Decimal("0.0299")
    assert line.amount == brl(expected)
    assert line.metadata.get("cap") == cap
    if cap == "minimum":
        assert line.metadata["uncapped_minor_units"] == "30"


def test_percentage_rounds_half_even() -> None:
    fee = Percentage(BRL, 50)
    assert price_lines(fee, brl(100))[0].amount == brl(0)  # 0.5 cent -> 0
    assert price_lines(fee, brl(300))[0].amount == brl(2)  # 1.5 cents -> 2


def test_percentage_rejects_quantities_and_other_currencies() -> None:
    with pytest.raises(ValidationFailed, match="Money amount"):
        price_lines(CARD_FEE, 100)
    with pytest.raises(ValidationFailed, match="USD"):
        price_lines(CARD_FEE, Money(100, USD))
    with pytest.raises(ValidationFailed, match=">= 0"):
        price_lines(CARD_FEE, brl(-1))


def test_quantity_models_reject_money() -> None:
    with pytest.raises(ValidationFailed, match="quantity"):
        price_lines(PerUnit(BRL, Decimal(1)), brl(100))


# --- flat fee, base fee + overage, minimum commitment --------------------------------------


def test_flat_fee_golden_and_prorated() -> None:
    fee = FlatFee(BRL, brl(9990))
    (line,) = price_lines(fee, 0)
    assert (line.key, line.quantity, line.unit_amount, line.amount) == (
        "pricing.flat_fee",
        1,
        Decimal("99.90"),
        brl(9990),
    )
    assert "proration" not in line.metadata
    # 9990 x 10 / 30 = 3330 exactly
    (line,) = price_lines(fee, 0, proration=Proration(10, 30))
    assert line.amount == brl(3330)
    assert line.metadata["proration"] == "10/30"
    # 9990 x 1 / 31 = 322.26 -> 322
    assert price_lines(fee, 0, proration=Proration(1, 31))[0].amount == brl(322)
    # full proration changes nothing
    assert price_lines(fee, 0, proration=Proration(30, 30))[0].metadata == {}


@pytest.mark.parametrize(
    ("quantity", "overage_units", "overage_amount"),
    [
        (1250, 250, 300),  # 250 x 0.012 = 3.00
        (1000, 0, 0),  # exactly the included units
        (500, 0, 0),
    ],
)
def test_base_fee_with_overage_golden(
    quantity: int, overage_units: int, overage_amount: int
) -> None:
    model = BaseFeeWithOverage(BRL, brl(4900), Decimal(1000), Decimal("0.012"))
    base, overage = price_lines(model, quantity)
    assert (base.key, base.amount) == ("pricing.base_fee", brl(4900))
    assert (overage.key, overage.quantity, overage.amount) == (
        "pricing.overage",
        overage_units,
        brl(overage_amount),
    )
    assert overage.metadata == {"included_units": "1000", "units": str(quantity)}


def test_base_fee_is_prorated_but_included_units_are_not() -> None:
    model = BaseFeeWithOverage(BRL, brl(4900), Decimal(1000), Decimal("0.012"))
    base, overage = price_lines(model, 1250, proration=Proration(15, 30))
    assert base.amount == brl(2450)
    assert overage.amount == brl(300)


def test_minimum_commitment_true_up() -> None:
    model = MinimumCommitment(BRL, brl(10000), PerUnit(BRL, Decimal("0.10")))
    usage, true_up = price_lines(model, 300)
    assert usage.amount == brl(3000)  # 300 x 0.10
    assert (true_up.key, true_up.amount) == ("pricing.minimum_commitment.true_up", brl(7000))
    assert true_up.metadata == {"minimum_minor_units": "10000", "usage_minor_units": "3000"}
    # above the minimum: no true-up line
    assert amounts(price_lines(model, 2000)) == [20000]
    # exactly the minimum: no true-up line either
    assert amounts(price_lines(model, 1000)) == [10000]


def test_minimum_commitment_is_prorated() -> None:
    model = MinimumCommitment(BRL, brl(10000), PerUnit(BRL, Decimal("0.10")))
    lines = price_lines(model, 300, proration=Proration(15, 30))
    assert amounts(lines) == [3000, 2000]  # minimum 50.00, usage 30.00
    assert lines[1].metadata["proration"] == "15/30"


# --- rate() ---------------------------------------------------------------------------------


def test_rate_pins_version_metric_and_rounding_policy() -> None:
    pinned = version(Graduated(BRL, TIERS), version=3)
    usage = AggregatedUsage(Decimal(1500), "api_calls", 2)
    charge = rate(pinned, usage, OCT)
    assert charge.price_id == pinned.price_id
    assert charge.price_version_id == pinned.id
    assert charge.price_version == 3
    assert charge.model == "graduated"
    assert charge.rounding == ROUNDING_POLICY == "line:ROUND_HALF_EVEN"
    assert (charge.metric_code, charge.metric_version) == ("api_calls", 2)
    assert charge.period == OCT
    assert charge.total == brl(10800)
    assert charge.total.minor_units == total(charge.lines)


def test_rate_accepts_plain_quantities_and_money() -> None:
    charge = rate(version(PerUnit(BRL, Decimal("0.25"))), 37, OCT)
    assert charge.total == brl(925)
    assert charge.metric_code is None
    assert rate(version(CARD_FEE), brl(10000), OCT).total == brl(299)


def test_rate_rejects_a_version_not_effective_in_the_period() -> None:
    old = version(
        PerUnit(BRL, Decimal(1)),
        effective_from=datetime(2026, 1, 1, tzinfo=UTC),
        effective_to=datetime(2026, 10, 1, tzinfo=UTC),
    )
    with pytest.raises(ValidationFailed, match="not effective"):
        rate(old, 1, OCT)


def test_rated_charge_total_must_equal_sum_of_lines() -> None:
    line = ChargeLine("pricing.per_unit", Decimal(1), Decimal(1), brl(100))
    with pytest.raises(ValidationFailed, match="sum of lines"):
        RatedCharge(uuid4(), uuid4(), 1, "per_unit", OCT, BRL, (line,), brl(99))


def test_charge_line_is_immutable_and_non_negative() -> None:
    line = ChargeLine("k", Decimal(1), Decimal(1), brl(1), {"a": "b"})
    with pytest.raises(TypeError):
        line.metadata["a"] = "c"  # pyright: ignore[reportIndexIssue]
    with pytest.raises(ValidationFailed, match=">= 0"):
        ChargeLine("k", Decimal(1), Decimal(1), brl(-1))


def test_aggregated_usage_validation() -> None:
    with pytest.raises(ValidationFailed, match="metric_version"):
        AggregatedUsage(Decimal(1), "m", 0)
    with pytest.raises(ValidationFailed, match="metric_code"):
        AggregatedUsage(Decimal(1), "", 1)
    with pytest.raises(ValidationFailed, match=">= 0"):
        AggregatedUsage(Decimal(-1), "m", 1)


def test_usage_rejects_float_and_bool() -> None:
    with pytest.raises(TypeError):
        price_lines(PerUnit(BRL, Decimal(1)), 1.5)  # pyright: ignore[reportArgumentType]
    with pytest.raises(TypeError):
        price_lines(PerUnit(BRL, Decimal(1)), True)
