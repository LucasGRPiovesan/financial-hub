from decimal import ROUND_HALF_EVEN, Decimal
from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from financial_hub.shared.money import (
    BHD,
    BRL,
    ISO_4217_EXPONENTS,
    JPY,
    Currency,
    Money,
    allocate,
    rate,
)

currencies = st.sampled_from([Currency.of(code) for code in sorted(ISO_4217_EXPONENTS)])
exp_0_2_3 = st.sampled_from([JPY, BRL, BHD])
minor_units = st.integers(min_value=-(10**15), max_value=10**15)
int_weights = st.integers(min_value=0, max_value=10**6)
decimal_weights = st.decimals(
    min_value=0, max_value=10**6, places=6, allow_nan=False, allow_infinity=False
)
weight_lists = st.lists(st.one_of(int_weights, decimal_weights), min_size=1, max_size=25).filter(
    lambda ws: any(w != 0 for w in ws)
)


def exact_shares(total: int, weights: list[int | Decimal]) -> list[Fraction]:
    fractions = [Fraction(w) for w in weights]
    weight_sum = sum(fractions, Fraction(0))
    return [Fraction(total) * w / weight_sum for w in fractions]


def half_even(value: Fraction) -> int:
    """Reference rounding implemented on exact rationals, independent of ``decimal``."""
    floor = value.numerator // value.denominator
    remainder = value - floor
    if remainder > Fraction(1, 2):
        return floor + 1
    if remainder < Fraction(1, 2):
        return floor
    return floor if floor % 2 == 0 else floor + 1


@given(currencies, minor_units, weight_lists)
def test_allocate_parts_sum_to_total(
    currency: Currency, units: int, weights: list[int | Decimal]
) -> None:
    parts = allocate(Money(units, currency), weights)

    assert len(parts) == len(weights)
    assert sum(p.minor_units for p in parts) == units
    assert all(p.currency == currency for p in parts)


@given(minor_units, weight_lists)
def test_allocate_each_part_within_one_minor_unit_of_exact_share(
    units: int, weights: list[int | Decimal]
) -> None:
    parts = allocate(Money(units, BRL), weights)

    for part, share in zip(parts, exact_shares(units, weights), strict=True):
        assert abs(Fraction(part.minor_units) - share) < 1


@given(minor_units, weight_lists)
def test_allocate_is_deterministic_and_symmetric(units: int, weights: list[int | Decimal]) -> None:
    first = allocate(Money(units, BRL), weights)

    assert allocate(Money(units, BRL), weights) == first
    assert allocate(Money(-units, BRL), weights) == [-p for p in first]


@given(minor_units, weight_lists)
def test_allocate_zero_weights_receive_nothing(units: int, weights: list[int | Decimal]) -> None:
    parts = allocate(Money(units, BRL), weights)

    for part, weight in zip(parts, weights, strict=True):
        if weight == 0:
            assert part.is_zero()


@given(st.floats(allow_nan=True, allow_infinity=True))
def test_money_never_accepts_float(value: float) -> None:
    with pytest.raises(TypeError):
        Money(value, BRL)  # pyright: ignore[reportArgumentType]
    with pytest.raises(TypeError):
        Money.from_decimal(value, BRL)  # pyright: ignore[reportArgumentType]
    with pytest.raises(TypeError):
        rate(value, Decimal(1), BRL)  # pyright: ignore[reportArgumentType]
    with pytest.raises(TypeError):
        rate(Decimal(1), value, BRL)  # pyright: ignore[reportArgumentType]
    with pytest.raises(TypeError):
        allocate(Money(100, BRL), [1, value])  # pyright: ignore[reportArgumentType]
    with pytest.raises(TypeError):
        Money(100, BRL) + value  # pyright: ignore[reportOperatorIssue, reportUnusedExpression]


@given(exp_0_2_3, minor_units)
def test_money_to_decimal_round_trip(currency: Currency, units: int) -> None:
    money = Money(units, currency)

    assert Money.from_decimal(money.to_decimal(), currency) == money
    assert Money.parse(str(money.to_decimal()), currency) == money


@given(exp_0_2_3, st.data())
def test_decimal_to_money_round_trip(currency: Currency, data: st.DataObject) -> None:
    amount = data.draw(
        st.decimals(
            min_value=-(10**12),
            max_value=10**12,
            places=currency.exponent,
            allow_nan=False,
            allow_infinity=False,
        )
    )

    assert Money.from_decimal(amount, currency).to_decimal() == amount


@given(
    currencies,
    st.decimals(min_value=-(10**9), max_value=10**9, places=6, allow_nan=False),
    st.decimals(min_value=0, max_value=10**6, places=18, allow_nan=False),
)
def test_rate_matches_exact_half_even_reference(
    currency: Currency, quantity: Decimal, unit_price: Decimal
) -> None:
    expected = half_even(Fraction(quantity) * Fraction(unit_price) * 10**currency.exponent)

    assert rate(quantity, unit_price, currency) == Money(expected, currency)


@given(currencies, st.decimals(allow_nan=False, allow_infinity=False, places=12))
def test_from_decimal_matches_exact_half_even_reference(
    currency: Currency, amount: Decimal
) -> None:
    expected = half_even(Fraction(amount) * 10**currency.exponent)

    assert Money.from_decimal(amount, currency, ROUND_HALF_EVEN).minor_units == expected
