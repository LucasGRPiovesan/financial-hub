"""Price models (ADR-0007, ADR-0002, FINANCIAL_MODEL s1).

Representation rules, enforced by every model on construction:

- Fixed amounts (fees, caps, minimums, tier flat fees, package prices) are ``Money``: integer
  minor units plus a currency, never negative, always in the model's currency.
- Unit prices are ``Decimal`` in **major units per unit** (``Decimal("0.0015")`` BRL per token),
  so sub-cent prices are exact. Quantities are ``Decimal`` too. Both must be finite, ``>= 0``,
  have at most ``SCALE`` (18) fractional digits and fewer than ``MAX_INTEGER_DIGITS`` (20)
  integer digits, which is exactly what fits ``NUMERIC(38,18)``. ``int`` is accepted and
  converted; text goes through ``parse_quantity`` / ``parse_unit_price``. ``float`` and
  ``bool`` are rejected everywhere.
- Tier bounds are inclusive upper bounds (``up_to``): a quantity exactly on a bound belongs to
  the lower tier. Bounds are strictly ascending and only the last tier is open-ended.

Models are immutable values. How they rate is in ``rating``.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Context, Decimal, Inexact, InvalidOperation
from enum import StrEnum
from typing import ClassVar, Final, cast

from financial_hub.shared.errors import FieldError, ValidationFailed
from financial_hub.shared.money import Currency, Money

SCALE: Final = 18
MAX_INTEGER_DIGITS: Final = 20
BASIS_POINTS: Final = 10_000

# Precise enough for any sum or difference of two NUMERIC(38,18) values; rounding is a bug.
EXACT: Final = Context(prec=80, traps=[InvalidOperation, Inexact])


def _invalid(pointer: str, detail: str) -> ValidationFailed:
    return ValidationFailed(f"{pointer}: {detail}", errors=[FieldError(pointer, detail)])


def _decimal(value: object, pointer: str) -> Decimal:
    """Validate a quantity or unit price (see the module docstring) and return it as Decimal."""
    if type(value) is int:
        number = Decimal(value)
    elif isinstance(value, Decimal):
        number = value
    else:
        raise TypeError(f"{pointer} must be Decimal or int, got {type(value).__name__}")
    if not number.is_finite():
        raise _invalid(pointer, "must be finite")
    if number < 0:
        raise _invalid(pointer, "must be >= 0")
    if number.is_zero():
        return Decimal(0)
    exponent = number.normalize(EXACT).as_tuple().exponent
    if isinstance(exponent, int) and -exponent > SCALE:
        raise _invalid(pointer, f"must have at most {SCALE} fractional digits")
    if number.adjusted() >= MAX_INTEGER_DIGITS:
        raise _invalid(pointer, f"must be below 10**{MAX_INTEGER_DIGITS}")
    return number


def _parse(text: str, pointer: str) -> Decimal:
    if not isinstance(text, str):  # pyright: ignore[reportUnnecessaryIsInstance] - runtime guard
        raise TypeError(f"{pointer} must be str, got {type(text).__name__}")
    try:
        number = Decimal(text.strip())
    except InvalidOperation:
        raise _invalid(pointer, f"not a decimal number: {text!r}") from None
    return _decimal(number, pointer)


def parse_quantity(text: str) -> Decimal:
    """Parse a usage quantity from text (``"1500"``, ``"0.25"``) exactly."""
    return _parse(text, "/quantity")


def parse_unit_price(text: str) -> Decimal:
    """Parse a major-unit price from text (``"0.0015"``) exactly."""
    return _parse(text, "/unit_price")


def check_quantity(value: object, pointer: str = "/quantity") -> Decimal:
    """Validate a quantity given as ``Decimal`` or ``int``."""
    return _decimal(value, pointer)


def _money(value: object, currency: Currency, pointer: str) -> Money:
    if not isinstance(value, Money):
        raise TypeError(f"{pointer} must be Money, got {type(value).__name__}")
    if value.currency != currency:
        raise _invalid(pointer, f"currency must be {currency.code}, got {value.currency.code}")
    if value.is_negative():
        raise _invalid(pointer, "must be >= 0")
    return value


def _currency(value: object) -> Currency:
    if not isinstance(value, Currency):
        raise TypeError(f"currency must be Currency, got {type(value).__name__}")
    return value


class PackageRounding(StrEnum):
    """How a partial package is billed: ``up`` bills it whole, ``down`` gives it away."""

    UP = "up"
    DOWN = "down"


@dataclass(frozen=True, slots=True)
class Tier:
    """One tier: ``up_to`` is the inclusive upper bound (``None`` = open-ended, last tier only).

    ``flat_fee`` is charged once when the tier receives a positive quantity (zero by default).
    The parent model checks bounds order and the fee currency.
    """

    up_to: Decimal | None
    unit_price: Decimal
    flat_fee: Money | None = None

    def __post_init__(self) -> None:
        if self.up_to is not None:
            object.__setattr__(self, "up_to", _decimal(self.up_to, "/up_to"))
        object.__setattr__(self, "unit_price", _decimal(self.unit_price, "/unit_price"))


def _tiers(currency: Currency, tiers: object) -> tuple[Tier, ...]:
    if not isinstance(tiers, (tuple, list)):
        raise TypeError("tiers must be a tuple or list of Tier")
    checked: list[Tier] = []
    previous: Decimal | None = None
    items = tuple(cast(Sequence[object], tiers))
    if not items:
        raise _invalid("/tiers", "at least one tier is required")
    last = len(items) - 1
    for index, tier in enumerate(items):
        if not isinstance(tier, Tier):
            raise TypeError(f"/tiers/{index} must be Tier, got {type(tier).__name__}")
        if tier.flat_fee is not None:
            _money(tier.flat_fee, currency, f"/tiers/{index}/flat_fee")
        if index == last:
            if tier.up_to is not None:
                raise _invalid(f"/tiers/{index}/up_to", "the last tier must be open-ended")
        elif tier.up_to is None:
            raise _invalid(f"/tiers/{index}/up_to", "only the last tier may be open-ended")
        elif tier.up_to <= (previous if previous is not None else Decimal(0)):
            raise _invalid(f"/tiers/{index}/up_to", "bounds must be > 0 and strictly ascending")
        previous = tier.up_to
        checked.append(tier)
    return tuple(checked)


@dataclass(frozen=True, slots=True)
class PerUnit:
    """``quantity x unit_price``."""

    kind: ClassVar[str] = "per_unit"
    currency: Currency
    unit_price: Decimal

    def __post_init__(self) -> None:
        _currency(self.currency)
        object.__setattr__(self, "unit_price", _decimal(self.unit_price, "/unit_price"))


@dataclass(frozen=True, slots=True)
class Package:
    """Blocks of ``package_size`` units at ``package_price`` each; ``rounding`` bills a part."""

    kind: ClassVar[str] = "package"
    currency: Currency
    package_size: int
    package_price: Money
    rounding: PackageRounding = PackageRounding.UP

    def __post_init__(self) -> None:
        _currency(self.currency)
        if type(self.package_size) is not int:
            raise TypeError(f"package_size must be int, got {type(self.package_size).__name__}")
        if self.package_size < 1:
            raise _invalid("/package_size", "must be >= 1")
        _money(self.package_price, self.currency, "/package_price")
        object.__setattr__(self, "rounding", PackageRounding(self.rounding))


@dataclass(frozen=True, slots=True)
class Graduated:
    """Each tier prices only the units that fall inside it (marginal pricing)."""

    kind: ClassVar[str] = "graduated"
    currency: Currency
    tiers: tuple[Tier, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "tiers", _tiers(_currency(self.currency), self.tiers))


@dataclass(frozen=True, slots=True)
class Volume:
    """The tier the total quantity lands in prices **all** units.

    Not monotonic by design when a higher tier is cheaper (the classic volume cliff: 100 units
    at 1.00 cost more than 101 units at 0.50). Monotonic when tier prices and fees never decrease.
    """

    kind: ClassVar[str] = "volume"
    currency: Currency
    tiers: tuple[Tier, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "tiers", _tiers(_currency(self.currency), self.tiers))


@dataclass(frozen=True, slots=True)
class Percentage:
    """``rate_bps`` basis points of a rated amount, clamped to ``[minimum, maximum]``.

    The minimum only applies to a positive amount: nothing transacted, nothing charged. Caps
    apply to each rated amount, so per-transaction caps mean rating each transaction.
    """

    kind: ClassVar[str] = "percentage"
    currency: Currency
    rate_bps: int
    minimum: Money | None = None
    maximum: Money | None = None

    def __post_init__(self) -> None:
        _currency(self.currency)
        if type(self.rate_bps) is not int:
            raise TypeError(f"rate_bps must be int, got {type(self.rate_bps).__name__}")
        if not 0 <= self.rate_bps <= BASIS_POINTS:
            raise _invalid("/rate_bps", f"must be between 0 and {BASIS_POINTS}")
        if self.minimum is not None:
            _money(self.minimum, self.currency, "/minimum")
        if self.maximum is not None:
            _money(self.maximum, self.currency, "/maximum")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise _invalid("/minimum", "must be <= maximum")


@dataclass(frozen=True, slots=True)
class FlatFee:
    """A recurring fee independent of usage (prorated for partial periods)."""

    kind: ClassVar[str] = "flat_fee"
    currency: Currency
    amount: Money

    def __post_init__(self) -> None:
        _money(self.amount, _currency(self.currency), "/amount")


@dataclass(frozen=True, slots=True)
class BaseFeeWithOverage:
    """Base fee (prorated) that includes ``included_units``; units above are ``overage``."""

    kind: ClassVar[str] = "base_fee_with_overage"
    currency: Currency
    base_fee: Money
    included_units: Decimal
    overage_unit_price: Decimal

    def __post_init__(self) -> None:
        _money(self.base_fee, _currency(self.currency), "/base_fee")
        object.__setattr__(self, "included_units", _decimal(self.included_units, "/included_units"))
        object.__setattr__(
            self, "overage_unit_price", _decimal(self.overage_unit_price, "/overage_unit_price")
        )


type UsageModel = PerUnit | Package | Graduated | Volume | Percentage | FlatFee | BaseFeeWithOverage

USAGE_MODELS: Final = (PerUnit, Package, Graduated, Volume, Percentage, FlatFee, BaseFeeWithOverage)


@dataclass(frozen=True, slots=True)
class MinimumCommitment:
    """Rates ``inner``; if its total is below ``minimum`` (prorated) a true-up line adds the gap."""

    kind: ClassVar[str] = "minimum_commitment"
    currency: Currency
    minimum: Money
    inner: UsageModel

    def __post_init__(self) -> None:
        _money(self.minimum, _currency(self.currency), "/minimum")
        if not isinstance(self.inner, USAGE_MODELS):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError(f"inner must be a usage model, got {type(self.inner).__name__}")
        if self.inner.currency != self.currency:
            raise _invalid("/inner/currency", f"must be {self.currency.code}")


type PriceModel = UsageModel | MinimumCommitment

PRICE_MODELS: Final = (*USAGE_MODELS, MinimumCommitment)
