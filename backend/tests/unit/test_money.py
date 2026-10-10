from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

import pytest

from financial_hub.shared.money import (
    BHD,
    BRL,
    CLP,
    JPY,
    KWD,
    USD,
    Currency,
    CurrencyMismatchError,
    Money,
    UnknownCurrencyError,
    allocate,
    rate,
)


class TestCurrency:
    @pytest.mark.parametrize(
        ("currency", "exponent"), [(BRL, 2), (USD, 2), (JPY, 0), (CLP, 0), (KWD, 3), (BHD, 3)]
    )
    def test_static_exponents(self, currency: Currency, exponent: int) -> None:
        assert currency.exponent == exponent

    def test_of_returns_equal_value_objects(self) -> None:
        assert Currency.of("BRL") == BRL
        assert str(BRL) == "BRL"

    def test_unknown_code_raises(self) -> None:
        with pytest.raises(UnknownCurrencyError):
            Currency.of("XXX")
        with pytest.raises(UnknownCurrencyError):
            Currency("brl", 2)

    def test_exponent_must_match_table(self) -> None:
        with pytest.raises(ValueError, match="exponent 2"):
            Currency("BRL", 3)

    def test_code_must_be_str(self) -> None:
        with pytest.raises(TypeError):
            Currency.of(986)  # pyright: ignore[reportArgumentType]


class TestMoney:
    def test_rejects_bool_and_non_currency(self) -> None:
        with pytest.raises(TypeError):
            Money(True, BRL)
        with pytest.raises(TypeError):
            Money(1, "BRL")  # pyright: ignore[reportArgumentType]

    def test_zero_and_predicates(self) -> None:
        assert Money.zero(BRL).is_zero()
        assert Money(1, BRL).is_positive()
        assert Money(-1, BRL).is_negative()
        assert not Money(-1, BRL).is_positive()

    def test_arithmetic(self) -> None:
        a, b = Money(1050, BRL), Money(250, BRL)

        assert a + b == Money(1300, BRL)
        assert a - b == Money(800, BRL)
        assert -a == Money(-1050, BRL)
        assert abs(Money(-5, BRL)) == Money(5, BRL)

    def test_ordering(self) -> None:
        small, big = Money(1, BRL), Money(2, BRL)

        assert small < big
        assert small <= big
        assert big > small
        assert big >= small
        assert small <= Money(1, BRL)
        assert sorted([big, small]) == [small, big]

    @pytest.mark.parametrize(
        "operation",
        [
            lambda a, b: a + b,
            lambda a, b: a - b,
            lambda a, b: a < b,
            lambda a, b: a <= b,
            lambda a, b: a > b,
            lambda a, b: a >= b,
        ],
    )
    def test_cross_currency_raises(self, operation: object) -> None:
        with pytest.raises(CurrencyMismatchError):
            operation(Money(1, BRL), Money(1, USD))  # pyright: ignore[reportCallIssue]

    def test_equality_includes_currency(self) -> None:
        assert Money(100, BRL) != Money(100, USD)
        assert Money(100, BRL) != 100

    @pytest.mark.parametrize("other", [1, Decimal(1), "1"])
    def test_non_money_operands_are_rejected(self, other: object) -> None:
        money = Money(1, BRL)
        with pytest.raises(TypeError):
            money + other  # pyright: ignore[reportOperatorIssue, reportUnusedExpression]
        with pytest.raises(TypeError):
            money - other  # pyright: ignore[reportOperatorIssue, reportUnusedExpression]
        with pytest.raises(TypeError):
            _ = money < other  # pyright: ignore[reportOperatorIssue]
        with pytest.raises(TypeError):
            _ = money <= other  # pyright: ignore[reportOperatorIssue]
        with pytest.raises(TypeError):
            _ = money > other  # pyright: ignore[reportOperatorIssue]
        with pytest.raises(TypeError):
            _ = money >= other  # pyright: ignore[reportOperatorIssue]

    def test_is_immutable(self) -> None:
        money = Money(1, BRL)
        with pytest.raises(AttributeError):
            money.minor_units = 2  # pyright: ignore[reportAttributeAccessIssue]

    def test_str(self) -> None:
        assert str(Money(10050, BRL)) == "100.50 BRL"
        assert str(Money(-5, KWD)) == "-0.005 KWD"
        assert str(Money(1200, JPY)) == "1200 JPY"


class TestDecimalConversion:
    def test_from_decimal_half_even_by_default(self) -> None:
        assert Money.from_decimal(Decimal("0.125"), BRL) == Money(12, BRL)
        assert Money.from_decimal(Decimal("0.135"), BRL) == Money(14, BRL)
        assert Money.from_decimal(Decimal("-0.125"), BRL) == Money(-12, BRL)

    def test_from_decimal_explicit_rounding(self) -> None:
        assert Money.from_decimal(Decimal("0.125"), BRL, ROUND_HALF_UP) == Money(13, BRL)
        assert Money.from_decimal(Decimal("0.129"), BRL, ROUND_DOWN) == Money(12, BRL)

    def test_from_decimal_handles_exponent_notation(self) -> None:
        assert Money.from_decimal(Decimal("1E+3"), JPY) == Money(1000, JPY)
        assert Money.from_decimal(Decimal("1.5E-2"), KWD) == Money(15, KWD)

    @pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
    def test_from_decimal_rejects_non_finite(self, value: Decimal) -> None:
        with pytest.raises(ValueError, match="finite"):
            Money.from_decimal(value, BRL)

    def test_from_decimal_rejects_int_and_str(self) -> None:
        with pytest.raises(TypeError):
            Money.from_decimal(1, BRL)  # pyright: ignore[reportArgumentType]
        with pytest.raises(TypeError):
            Money.from_decimal("1.00", BRL)  # pyright: ignore[reportArgumentType]

    def test_to_decimal_keeps_currency_scale(self) -> None:
        assert Money(10050, BRL).to_decimal().as_tuple().exponent == -2
        assert str(Money(0, KWD).to_decimal()) == "0.000"
        assert str(Money(7, JPY).to_decimal()) == "7"

    def test_parse_is_exact(self) -> None:
        assert Money.parse("100.5", BRL) == Money(10050, BRL)
        assert Money.parse(" 0.001 ", KWD) == Money(1, KWD)
        assert Money.parse("-3", JPY) == Money(-3, JPY)

    @pytest.mark.parametrize("text", ["0.001", "1.005"])
    def test_parse_rejects_extra_precision(self, text: str) -> None:
        with pytest.raises(ValueError, match="decimals"):
            Money.parse(text, BRL)

    @pytest.mark.parametrize("text", ["abc", "", "1,00"])
    def test_parse_rejects_garbage(self, text: str) -> None:
        with pytest.raises(ValueError, match="not a decimal"):
            Money.parse(text, BRL)

    @pytest.mark.parametrize("text", ["NaN", "Infinity"])
    def test_parse_rejects_non_finite(self, text: str) -> None:
        with pytest.raises(ValueError, match="finite"):
            Money.parse(text, BRL)

    def test_parse_requires_str(self) -> None:
        with pytest.raises(TypeError):
            Money.parse(Decimal("1"), BRL)  # pyright: ignore[reportArgumentType]


class TestRate:
    def test_quantizes_once_half_even(self) -> None:
        # 3 x 0.125 = 0.375 -> 38 (half-even of 37.5); rounding each unit first would give 36.
        assert rate(Decimal(3), Decimal("0.125"), BRL) == Money(38, BRL)
        assert rate(Decimal(1), Decimal("0.125"), BRL) == Money(12, BRL)

    def test_high_precision_unit_price(self) -> None:
        price = Decimal("0.000123456789012345678")
        # 1e6 x price = 123.456789012345678 USD -> 12346 cents (one rounding, at the end).
        assert rate(Decimal(1_000_000), price, USD) == Money(12346, USD)

    def test_negative_quantity(self) -> None:
        assert rate(Decimal(-3), Decimal("0.125"), BRL) == Money(-38, BRL)

    def test_rejects_non_finite(self) -> None:
        with pytest.raises(ValueError):
            rate(Decimal("NaN"), Decimal(1), BRL)


class TestAllocate:
    def test_even_split_gives_leftover_to_lowest_index(self) -> None:
        parts = allocate(Money(100, BRL), [1, 1, 1])

        assert [p.minor_units for p in parts] == [34, 33, 33]

    def test_largest_remainder_wins(self) -> None:
        # Exact shares 16.6.., 33.3.., 50 -> floors 16, 33, 50 (+1 leftover to the 0.66 remainder).
        parts = allocate(Money(100, BRL), [1, 2, 3])

        assert [p.minor_units for p in parts] == [17, 33, 50]

    def test_negative_total_is_symmetric(self) -> None:
        parts = allocate(Money(-100, BRL), [1, 1, 1])

        assert [p.minor_units for p in parts] == [-34, -33, -33]

    def test_zero_weights_get_zero(self) -> None:
        parts = allocate(Money(101, BRL), [0, 1, 0, 1])

        assert [p.minor_units for p in parts] == [0, 51, 0, 50]

    def test_decimal_weights(self) -> None:
        parts = allocate(Money(1000, BRL), [Decimal("0.9"), Decimal("0.1")])

        assert [p.minor_units for p in parts] == [900, 100]

    def test_zero_total(self) -> None:
        assert allocate(Money(0, JPY), [1, 2]) == [Money(0, JPY), Money(0, JPY)]

    def test_all_zero_weights_raise(self) -> None:
        with pytest.raises(ValueError, match="> 0"):
            allocate(Money(100, BRL), [0, 0])

    def test_empty_weights_raise(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            allocate(Money(100, BRL), [])
        with pytest.raises(ValueError, match="non-empty"):
            allocate(Money(100, BRL), "12")  # pyright: ignore[reportArgumentType]

    def test_negative_weight_raises(self) -> None:
        with pytest.raises(ValueError, match=">= 0"):
            allocate(Money(100, BRL), [1, -1])

    @pytest.mark.parametrize("weight", [True, 1.0, "1", None])
    def test_non_int_or_decimal_weight_raises(self, weight: object) -> None:
        with pytest.raises(TypeError):
            allocate(Money(100, BRL), [1, weight])  # pyright: ignore[reportArgumentType]

    def test_non_finite_decimal_weight_raises(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            allocate(Money(100, BRL), [Decimal(1), Decimal("Infinity")])
