"""Property tests for rating: monotonicity, line sums, tier bounds, zero usage, exact rounding."""

from decimal import Decimal
from fractions import Fraction

from hypothesis import given
from hypothesis import strategies as st

from financial_hub.modules.pricing.domain import (
    BaseFeeWithOverage,
    FlatFee,
    Graduated,
    MinimumCommitment,
    Package,
    PackageRounding,
    Percentage,
    PerUnit,
    PriceModel,
    Proration,
    Tier,
    Volume,
    price_lines,
)
from financial_hub.shared.money import BRL, Money

quantities = st.decimals(min_value=0, max_value=10**7, places=4, allow_nan=False)
unit_prices = st.decimals(min_value=0, max_value=100, places=6, allow_nan=False)
fees = st.integers(min_value=0, max_value=10**6).map(lambda units: Money(units, BRL))
optional_fees = st.one_of(st.none(), fees)


@st.composite
def tier_lists(draw: st.DrawFn, *, non_decreasing: bool = False) -> tuple[Tier, ...]:
    bounds = sorted(
        draw(st.sets(st.integers(min_value=1, max_value=10**6), min_size=0, max_size=4))
    )
    prices = [draw(unit_prices) for _ in range(len(bounds) + 1)]
    flat = [draw(optional_fees) for _ in range(len(bounds) + 1)]
    if non_decreasing:
        prices.sort()
        flat = [Money(units, BRL) for units in sorted(f.minor_units if f else 0 for f in flat)]
    upper: list[Decimal | None] = [Decimal(b) for b in bounds] + [None]
    return tuple(Tier(u, p, f) for u, p, f in zip(upper, prices, flat, strict=True))


def total(model: PriceModel, usage: Decimal | Money, proration: Proration | None = None) -> int:
    lines = price_lines(model, usage, proration=proration)
    assert all(line.amount.currency == BRL for line in lines)
    assert all(line.amount.minor_units >= 0 for line in lines)
    return sum(line.amount.minor_units for line in lines)


def half_even(value: Fraction) -> int:
    floor = value.numerator // value.denominator
    remainder = value - floor
    if remainder > Fraction(1, 2) or (remainder == Fraction(1, 2) and floor % 2 == 1):
        return floor + 1
    return floor


def cents(quantity: Decimal, unit_price: Decimal) -> int:
    """Reference line amount on exact rationals, independent of ``decimal``."""
    return half_even(Fraction(quantity) * Fraction(unit_price) * 100)


usage_models: st.SearchStrategy[PriceModel] = st.one_of(
    st.builds(PerUnit, st.just(BRL), unit_prices),
    st.builds(
        Package,
        st.just(BRL),
        st.integers(min_value=1, max_value=10**4),
        fees,
        st.sampled_from(PackageRounding),
    ),
    tier_lists().map(lambda tiers: Graduated(BRL, tiers)),
    tier_lists(non_decreasing=True).map(lambda tiers: Volume(BRL, tiers)),
    st.builds(BaseFeeWithOverage, st.just(BRL), fees, quantities, unit_prices),
    st.builds(FlatFee, st.just(BRL), fees),
)
monotonic_models: st.SearchStrategy[PriceModel] = st.one_of(
    usage_models,
    st.builds(MinimumCommitment, st.just(BRL), fees, usage_models),  # pyright: ignore[reportArgumentType]
)


@given(monotonic_models, quantities, quantities)
def test_more_usage_never_costs_less(model: PriceModel, a: Decimal, b: Decimal) -> None:
    low, high = sorted((a, b))
    assert total(model, low) <= total(model, high)


@given(
    st.integers(min_value=0, max_value=10**4),
    optional_fees,
    optional_fees,
    st.integers(min_value=0, max_value=10**9),
    st.integers(min_value=0, max_value=10**9),
)
def test_percentage_is_monotonic(
    bps: int, low_cap: Money | None, high_cap: Money | None, a: int, b: int
) -> None:
    if low_cap is not None and high_cap is not None and low_cap > high_cap:
        low_cap, high_cap = high_cap, low_cap
    fee = Percentage(BRL, bps, minimum=low_cap, maximum=high_cap)
    low, high = sorted((a, b))
    assert total(fee, Money(low, BRL)) <= total(fee, Money(high, BRL))


@given(tier_lists(), quantities, quantities)
def test_volume_is_monotonic_within_a_tier(tiers: tuple[Tier, ...], a: Decimal, b: Decimal) -> None:
    low, high = sorted((a, b))
    lines_low = price_lines(Volume(BRL, tiers), low)
    lines_high = price_lines(Volume(BRL, tiers), high)
    if lines_low[0].metadata["tier"] == lines_high[0].metadata["tier"] and low > 0:
        assert total(Volume(BRL, tiers), low) <= total(Volume(BRL, tiers), high)


@given(tier_lists(), quantities)
def test_graduated_matches_a_rational_reference(tiers: tuple[Tier, ...], quantity: Decimal) -> None:
    expected = 0
    lower = Decimal(0)
    for tier in tiers:
        top = quantity if tier.up_to is None else min(quantity, tier.up_to)
        units = max(top - lower, Decimal(0))
        expected += cents(units, tier.unit_price)
        if units > 0 and tier.flat_fee is not None:
            expected += tier.flat_fee.minor_units
        if tier.up_to is None or quantity <= tier.up_to:
            break
        lower = tier.up_to
    assert total(Graduated(BRL, tiers), quantity) == expected


@given(tier_lists())
def test_graduated_at_each_bound_reaches_no_further_tier(tiers: tuple[Tier, ...]) -> None:
    for index, tier in enumerate(tiers[:-1], start=1):
        assert tier.up_to is not None
        lines = price_lines(Graduated(BRL, tiers), tier.up_to)
        assert max(int(line.metadata["tier"]) for line in lines) == index
        assert sum(line.quantity for line in lines if line.key.endswith(".tier")) == tier.up_to


@given(tier_lists())
def test_volume_at_each_bound_uses_the_lower_tier(tiers: tuple[Tier, ...]) -> None:
    for index, tier in enumerate(tiers[:-1], start=1):
        assert tier.up_to is not None
        (line, *_) = price_lines(Volume(BRL, tiers), tier.up_to)
        assert line.metadata["tier"] == str(index)
        assert line.amount.minor_units == cents(tier.up_to, tier.unit_price)


@given(usage_models)
def test_zero_usage_costs_only_fixed_fees(model: PriceModel) -> None:
    fixed = 0
    if isinstance(model, FlatFee):
        fixed = model.amount.minor_units
    elif isinstance(model, BaseFeeWithOverage):
        fixed = model.base_fee.minor_units
    assert total(model, Decimal(0)) == fixed


@given(unit_prices, quantities)
def test_per_unit_matches_a_rational_reference(unit_price: Decimal, quantity: Decimal) -> None:
    assert total(PerUnit(BRL, unit_price), quantity) == cents(quantity, unit_price)


@given(
    fees,
    st.integers(min_value=1, max_value=366).flatmap(
        lambda days: st.tuples(st.integers(min_value=0, max_value=days), st.just(days))
    ),
)
def test_proration_is_within_half_a_unit_and_bounded(fee: Money, days: tuple[int, int]) -> None:
    proration = Proration(*days)
    prorated = proration.apply(fee).minor_units
    exact = Fraction(fee.minor_units * days[0], days[1])
    assert abs(prorated - exact) <= Fraction(1, 2)
    assert prorated == half_even(exact)
    assert 0 <= prorated <= fee.minor_units


@given(monotonic_models, quantities, st.integers(min_value=1, max_value=31))
def test_proration_never_increases_the_charge(
    model: PriceModel, quantity: Decimal, period_days: int
) -> None:
    partial = Proration(period_days // 2, period_days)
    assert total(model, quantity, partial) <= total(model, quantity)
