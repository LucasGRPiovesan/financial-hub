from decimal import Decimal

import pytest

from financial_hub.modules.pricing.domain import (
    BaseFeeWithOverage,
    FlatFee,
    Graduated,
    MinimumCommitment,
    Package,
    PackageRounding,
    Percentage,
    PerUnit,
    Tier,
    Volume,
    check_quantity,
    parse_quantity,
    parse_unit_price,
)
from financial_hub.shared.errors import ValidationFailed
from financial_hub.shared.money import BRL, USD, Money


def brl(units: int) -> Money:
    return Money(units, BRL)


OPEN = Tier(None, Decimal("0.05"))


def test_parse_quantity_and_unit_price_are_exact() -> None:
    assert parse_quantity(" 1500 ") == Decimal(1500)
    assert parse_quantity("0.000000000000000001") == Decimal("1E-18")
    assert parse_unit_price("0.0015") == Decimal("0.0015")
    assert parse_quantity("-0") == Decimal(0)
    assert parse_quantity("1.50000000000000000000000") == Decimal("1.5")  # trailing zeros ok
    assert check_quantity(7) == Decimal(7)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("abc", "not a decimal"),
        ("-1", ">= 0"),
        ("NaN", "finite"),
        ("Infinity", "finite"),
        ("0.0000000000000000001", "18 fractional"),
        ("100000000000000000000", "below 10"),
    ],
)
def test_parse_rejects_invalid_text(text: str, message: str) -> None:
    with pytest.raises(ValidationFailed, match=message):
        parse_quantity(text)


@pytest.mark.parametrize("value", [1.5, True, "1", None])
def test_check_quantity_rejects_non_decimal_types(value: object) -> None:
    with pytest.raises(TypeError):
        check_quantity(value)


def test_parse_rejects_non_str() -> None:
    with pytest.raises(TypeError):
        parse_unit_price(1)  # pyright: ignore[reportArgumentType]


def test_per_unit_validation() -> None:
    assert PerUnit(BRL, 2).unit_price == Decimal(2)  # pyright: ignore[reportArgumentType]
    with pytest.raises(ValidationFailed, match=">= 0"):
        PerUnit(BRL, Decimal(-1))
    with pytest.raises(TypeError):
        PerUnit(BRL, 0.1)  # pyright: ignore[reportArgumentType]
    with pytest.raises(TypeError):
        PerUnit("BRL", Decimal(1))  # pyright: ignore[reportArgumentType]


def test_package_validation() -> None:
    assert Package(BRL, 10, brl(5), "down").rounding is PackageRounding.DOWN  # pyright: ignore[reportArgumentType]
    with pytest.raises(ValidationFailed, match=">= 1"):
        Package(BRL, 0, brl(5))
    with pytest.raises(TypeError):
        Package(BRL, Decimal(10), brl(5))  # pyright: ignore[reportArgumentType]
    with pytest.raises(ValidationFailed, match="currency must be BRL"):
        Package(BRL, 10, Money(5, USD))
    with pytest.raises(ValidationFailed, match=">= 0"):
        Package(BRL, 10, brl(-5))
    with pytest.raises(TypeError):
        Package(BRL, 10, 5)  # pyright: ignore[reportArgumentType]
    with pytest.raises(ValueError, match="sideways"):
        Package(BRL, 10, brl(5), "sideways")  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize("model", [Graduated, Volume])
def test_tier_validation(model: type[Graduated] | type[Volume]) -> None:
    tiers = [Tier(Decimal(10), Decimal(1)), OPEN]
    assert model(BRL, tiers).tiers == tuple(tiers)  # pyright: ignore[reportArgumentType]
    cases: list[tuple[object, str]] = [
        ((), "at least one tier"),
        ((Tier(Decimal(10), Decimal(1)),), "last tier must be open-ended"),
        ((OPEN, OPEN), "only the last tier"),
        ((Tier(Decimal(10), Decimal(1)), Tier(Decimal(10), Decimal(1)), OPEN), "ascending"),
        ((Tier(Decimal(10), Decimal(1)), Tier(Decimal(5), Decimal(1)), OPEN), "ascending"),
        ((Tier(Decimal(0), Decimal(1)), OPEN), "ascending"),
        ((Tier(None, Decimal(1), flat_fee=Money(1, USD)),), "currency must be BRL"),
        ((Tier(None, Decimal(1), flat_fee=brl(-1)),), ">= 0"),
    ]
    for bad, message in cases:
        with pytest.raises(ValidationFailed, match=message):
            model(BRL, bad)  # pyright: ignore[reportArgumentType]
    with pytest.raises(TypeError):
        model(BRL, "tiers")  # pyright: ignore[reportArgumentType]
    with pytest.raises(TypeError):
        model(BRL, (Decimal(1),))  # pyright: ignore[reportArgumentType]
    with pytest.raises(ValidationFailed, match=">= 0"):
        Tier(Decimal(-1), Decimal(1))
    with pytest.raises(ValidationFailed, match=">= 0"):
        Tier(None, Decimal(-1))


def test_percentage_validation() -> None:
    assert Percentage(BRL, 10_000).rate_bps == 10_000
    for bps in (-1, 10_001):
        with pytest.raises(ValidationFailed, match="rate_bps"):
            Percentage(BRL, bps)
    with pytest.raises(TypeError):
        Percentage(BRL, Decimal("2.5"))  # pyright: ignore[reportArgumentType]
    with pytest.raises(ValidationFailed, match="minimum"):
        Percentage(BRL, 100, minimum=brl(10), maximum=brl(5))
    with pytest.raises(ValidationFailed, match="currency"):
        Percentage(BRL, 100, maximum=Money(5, USD))
    with pytest.raises(ValidationFailed, match="currency"):
        Percentage(BRL, 100, minimum=Money(5, USD))


def test_fixed_fee_models_validation() -> None:
    with pytest.raises(ValidationFailed, match=">= 0"):
        FlatFee(BRL, brl(-1))
    with pytest.raises(ValidationFailed, match="currency"):
        BaseFeeWithOverage(BRL, Money(1, USD), Decimal(1), Decimal(1))
    with pytest.raises(ValidationFailed, match=">= 0"):
        BaseFeeWithOverage(BRL, brl(1), Decimal(-1), Decimal(1))
    inner = PerUnit(BRL, Decimal(1))
    with pytest.raises(ValidationFailed, match="currency"):
        MinimumCommitment(BRL, Money(1, USD), inner)
    with pytest.raises(ValidationFailed, match="inner/currency"):
        MinimumCommitment(BRL, brl(1), PerUnit(USD, Decimal(1)))
    with pytest.raises(TypeError):
        MinimumCommitment(BRL, brl(1), MinimumCommitment(BRL, brl(1), inner))  # pyright: ignore[reportArgumentType]


def test_models_are_frozen() -> None:
    model = PerUnit(BRL, Decimal(1))
    with pytest.raises(AttributeError):
        model.unit_price = Decimal(2)  # pyright: ignore[reportAttributeAccessIssue]
    assert model.kind == "per_unit"
