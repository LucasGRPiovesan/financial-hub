"""Money value object, ISO 4217 currencies, rating and allocation (ADR-0002, FINANCIAL_MODEL s1).

Amounts are integer minor units plus a currency. ``Decimal`` is only used for rates and unit
prices and is quantized once, with ``ROUND_HALF_EVEN`` unless stated otherwise. ``float`` is
rejected everywhere.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal, Inexact, InvalidOperation
from fractions import Fraction
from typing import Final, Self

# ISO 4217 minor-unit exponents. Static on purpose: changing one is a data migration, not config.
ISO_4217_EXPONENTS: Final[dict[str, int]] = {
    # 0 decimals
    "BIF": 0, "CLP": 0, "DJF": 0, "GNF": 0, "ISK": 0, "JPY": 0, "KMF": 0, "KRW": 0, "PYG": 0,
    "RWF": 0, "UGX": 0, "VND": 0, "VUV": 0, "XAF": 0, "XOF": 0, "XPF": 0,
    # 2 decimals
    "AED": 2, "ARS": 2, "AUD": 2, "BOB": 2, "BRL": 2, "CAD": 2, "CHF": 2, "CNY": 2, "COP": 2,
    "CZK": 2, "DKK": 2, "EUR": 2, "GBP": 2, "HKD": 2, "HUF": 2, "IDR": 2, "ILS": 2, "INR": 2,
    "MXN": 2, "NOK": 2, "NZD": 2, "PEN": 2, "PHP": 2, "PLN": 2, "RON": 2, "SAR": 2, "SEK": 2,
    "SGD": 2, "THB": 2, "TRY": 2, "TWD": 2, "USD": 2, "UYU": 2, "ZAR": 2,
    # 3 decimals
    "BHD": 3, "IQD": 3, "JOD": 3, "KWD": 3, "LYD": 3, "OMR": 3, "TND": 3,
    # 4 decimals
    "CLF": 4, "UYW": 4,
}  # fmt: skip


class UnknownCurrencyError(ValueError):
    """The code is not an ISO 4217 currency known to the static table."""


class CurrencyMismatchError(ValueError):
    """Arithmetic or comparison between amounts in different currencies (no FX, by design)."""


@dataclass(frozen=True, slots=True)
class Currency:
    """An ISO 4217 currency. The exponent must match the static table."""

    code: str
    exponent: int

    def __post_init__(self) -> None:
        expected = ISO_4217_EXPONENTS.get(self.code)
        if expected is None:
            raise UnknownCurrencyError(f"unknown ISO 4217 currency: {self.code!r}")
        if type(self.exponent) is not int or self.exponent != expected:
            raise ValueError(f"{self.code} has exponent {expected}, got {self.exponent!r}")

    @classmethod
    def of(cls, code: str) -> Self:
        if not isinstance(code, str):  # pyright: ignore[reportUnnecessaryIsInstance] - runtime guard
            raise TypeError(f"currency code must be str, got {type(code).__name__}")
        exponent = ISO_4217_EXPONENTS.get(code)
        if exponent is None:
            raise UnknownCurrencyError(f"unknown ISO 4217 currency: {code!r}")
        return cls(code, exponent)

    def __str__(self) -> str:
        return self.code


BRL: Final = Currency.of("BRL")
USD: Final = Currency.of("USD")
EUR: Final = Currency.of("EUR")
GBP: Final = Currency.of("GBP")
JPY: Final = Currency.of("JPY")
CLP: Final = Currency.of("CLP")
KWD: Final = Currency.of("KWD")
BHD: Final = Currency.of("BHD")


def _require_decimal(value: object, name: str) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal, got {type(value).__name__}")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite, got {value}")
    return value


def _exact_context(*values: Decimal) -> Context:
    """A context precise enough that the product (or a rescale) of ``values`` is exact."""
    digits = sum(len(v.as_tuple().digits) for v in values)
    # Inexact is trapped: any silent rounding inside this context is a bug, not a result.
    return Context(prec=digits + 2, traps=[InvalidOperation, Inexact])


def _to_integer(value: Decimal, rounding: str) -> int:
    """Quantize ``value`` to an integer exactly once, with the given rounding mode."""
    context = Context(prec=max(value.adjusted() + 1, 1) + 2, traps=[InvalidOperation])
    return int(value.quantize(Decimal(1), rounding=rounding, context=context))


@dataclass(frozen=True, slots=True)
class Money:
    """Immutable amount in integer minor units. Cross-currency operations raise."""

    minor_units: int
    currency: Currency

    def __post_init__(self) -> None:
        # bool is an int subclass and float is never acceptable in money paths.
        if type(self.minor_units) is not int:
            raise TypeError(f"minor_units must be int, got {type(self.minor_units).__name__}")
        if not isinstance(self.currency, Currency):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError(f"currency must be Currency, got {type(self.currency).__name__}")

    # --- constructors -----------------------------------------------------------------------
    @classmethod
    def zero(cls, currency: Currency) -> Self:
        return cls(0, currency)

    @classmethod
    def from_decimal(
        cls, amount: Decimal, currency: Currency, rounding: str = ROUND_HALF_EVEN
    ) -> Self:
        """Convert a major-unit ``Decimal`` to minor units, quantizing once with ``rounding``."""
        amount = _require_decimal(amount, "amount")
        scaled = amount.scaleb(currency.exponent, context=_exact_context(amount))
        return cls(_to_integer(scaled, rounding), currency)

    @classmethod
    def parse(cls, text: str, currency: Currency) -> Self:
        """Parse a provider decimal string (``"100.5"``) exactly; raises if rounding is needed."""
        if not isinstance(text, str):  # pyright: ignore[reportUnnecessaryIsInstance] - runtime guard
            raise TypeError(f"text must be str, got {type(text).__name__}")
        try:
            amount = Decimal(text.strip())
        except InvalidOperation:
            raise ValueError(f"not a decimal amount: {text!r}") from None
        money = cls.from_decimal(_require_decimal(amount, "amount"), currency)
        if money.to_decimal() != amount:
            raise ValueError(f"{text!r} has more than {currency.exponent} decimals for {currency}")
        return money

    def to_decimal(self) -> Decimal:
        """Exact major-unit value, scaled to the currency exponent (``10050 BRL -> 100.50``)."""
        sign = 1 if self.minor_units < 0 else 0
        digits = tuple(int(d) for d in str(abs(self.minor_units)))
        return Decimal((sign, digits, -self.currency.exponent))

    # --- predicates -------------------------------------------------------------------------
    def is_zero(self) -> bool:
        return self.minor_units == 0

    def is_positive(self) -> bool:
        return self.minor_units > 0

    def is_negative(self) -> bool:
        return self.minor_units < 0

    # --- arithmetic -------------------------------------------------------------------------
    def _check(self, other: Money) -> None:
        if other.currency != self.currency:
            raise CurrencyMismatchError(
                f"cannot combine {self.currency.code} and {other.currency.code}"
            )

    def __add__(self, other: object) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        self._check(other)
        return Money(self.minor_units + other.minor_units, self.currency)

    def __sub__(self, other: object) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        self._check(other)
        return Money(self.minor_units - other.minor_units, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.minor_units, self.currency)

    def __abs__(self) -> Money:
        return Money(abs(self.minor_units), self.currency)

    # --- ordering (equality is the dataclass one: same units and same currency) -------------
    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        self._check(other)
        return self.minor_units < other.minor_units

    def __le__(self, other: object) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        self._check(other)
        return self.minor_units <= other.minor_units

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        self._check(other)
        return self.minor_units > other.minor_units

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        self._check(other)
        return self.minor_units >= other.minor_units

    def __str__(self) -> str:
        return f"{self.to_decimal()} {self.currency.code}"


def rate(quantity: Decimal, unit_price: Decimal, currency: Currency) -> Money:
    """Rated amount ``quantity x unit_price``: exact product, quantized once (ROUND_HALF_EVEN)."""
    quantity = _require_decimal(quantity, "quantity")
    unit_price = _require_decimal(unit_price, "unit_price")
    product = _exact_context(quantity, unit_price).multiply(quantity, unit_price)
    return Money.from_decimal(product, currency, ROUND_HALF_EVEN)


def _weight(value: object, index: int) -> Fraction:
    # bool is rejected explicitly: True/False as weights is always a caller bug.
    if type(value) is int:
        weight = Fraction(value)
    elif isinstance(value, Decimal):
        weight = Fraction(_require_decimal(value, f"weights[{index}]"))
    else:
        raise TypeError(f"weights[{index}] must be int or Decimal, got {type(value).__name__}")
    if weight < 0:
        raise ValueError(f"weights[{index}] must be >= 0, got {value}")
    return weight


def allocate(total: Money, weights: Sequence[int | Decimal]) -> list[Money]:
    """Split ``total`` proportionally to ``weights`` with the largest-remainder method.

    The parts always sum to ``total``; each part is within one minor unit of its exact share.
    Leftover units go to the largest fractional remainders, ties broken by lower index first
    (callers list the platform last, FINANCIAL_MODEL section 1). Zero weights receive zero.
    Negative totals are symmetric: ``allocate(-t, w) == [-p for p in allocate(t, w)]``.
    """
    if isinstance(weights, (str, bytes)) or len(weights) == 0:
        raise ValueError("weights must be a non-empty sequence")
    exact_weights = [_weight(w, i) for i, w in enumerate(weights)]
    weight_sum = sum(exact_weights, Fraction(0))
    if weight_sum == 0:
        raise ValueError("at least one weight must be > 0")

    sign = -1 if total.minor_units < 0 else 1
    magnitude = abs(total.minor_units)
    shares = [magnitude * w / weight_sum for w in exact_weights]
    floors = [share.numerator // share.denominator for share in shares]
    leftover = magnitude - sum(floors)
    # leftover < number of parts with a positive remainder, so zero-remainder parts never get one.
    order = sorted(range(len(shares)), key=lambda i: (-(shares[i] - floors[i]), i))
    for i in order[:leftover]:
        floors[i] += 1
    return [Money(sign * units, total.currency) for units in floors]
