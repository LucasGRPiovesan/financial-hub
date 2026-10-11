"""Rating: (price version, usage, period) -> charge lines with a breakdown (ADR-0007).

Rounding policy (``ROUNDING_POLICY``, pinned on every ``RatedCharge``): each line's amount is
computed exactly and quantized **once per line** to minor units with ``ROUND_HALF_EVEN``
(ADR-0002, FINANCIAL_MODEL s1). Totals are integer sums of line amounts, never re-rounded.
Prorated fixed amounts use the same half-even rule on integers (``Proration.apply``).

Line conventions:

- ``unit_amount`` is in major units per unit and, except for prorated lines,
  ``amount == round_half_even(quantity x unit_amount)``. Prorated lines keep the full unit amount
  and record ``proration`` (``"active/period"`` days) in their metadata.
- Graduated tiers emit one line per tier reached (the first tier always, so zero usage still
  yields a zero line). A tier flat fee is a separate line, charged only when the tier receives a
  positive quantity; the same applies to volume tiers.
- Only fixed recurring amounts are prorated (flat fee, base fee, minimum commitment); included
  units and usage are not.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from types import MappingProxyType
from typing import Final
from uuid import UUID

from financial_hub.modules.pricing.domain.model import (
    EXACT,
    BaseFeeWithOverage,
    FlatFee,
    Graduated,
    MinimumCommitment,
    Package,
    PackageRounding,
    Percentage,
    PerUnit,
    PriceModel,
    Tier,
    UsageModel,
    Volume,
    check_quantity,
)
from financial_hub.modules.pricing.domain.period import BillingPeriod, Proration
from financial_hub.modules.pricing.domain.versions import PriceVersion
from financial_hub.shared.errors import ValidationFailed
from financial_hub.shared.money import Currency, Money
from financial_hub.shared.money import rate as rate_amount

ROUNDING_POLICY: Final = "line:ROUND_HALF_EVEN"


@dataclass(frozen=True, slots=True)
class AggregatedUsage:
    """A metered quantity for the period, pinned to the metric definition that produced it."""

    quantity: Decimal
    metric_code: str
    metric_version: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "quantity", check_quantity(self.quantity))
        if type(self.metric_version) is not int or self.metric_version < 1:
            raise ValidationFailed("metric_version must be an int >= 1")
        if not isinstance(self.metric_code, str) or not self.metric_code:  # pyright: ignore[reportUnnecessaryIsInstance]
            raise ValidationFailed("metric_code must be a non-empty string")


type Usage = Decimal | int | Money | AggregatedUsage


@dataclass(frozen=True, slots=True)
class ChargeLine:
    """One component of a rated charge. ``key`` is a stable description key (i18n-able)."""

    key: str
    quantity: Decimal
    unit_amount: Decimal
    amount: Money
    metadata: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if self.amount.is_negative():
            raise ValidationFailed("charge line amounts must be >= 0")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class RatedCharge:
    """Rating result. ``total`` is always the integer sum of the line amounts."""

    price_id: UUID
    price_version_id: UUID
    price_version: int
    model: str
    period: BillingPeriod
    currency: Currency
    lines: tuple[ChargeLine, ...]
    total: Money
    rounding: str = ROUNDING_POLICY
    metric_code: str | None = None
    metric_version: int | None = None

    def __post_init__(self) -> None:
        lines = tuple(self.lines)
        object.__setattr__(self, "lines", lines)
        expected = sum((line.amount for line in lines), Money.zero(self.currency))
        if self.total != expected:
            raise ValidationFailed(f"total {self.total} != sum of lines {expected}")


def _line(
    key: str,
    quantity: Decimal,
    unit_amount: Decimal,
    currency: Currency,
    metadata: Mapping[str, str] | None = None,
) -> ChargeLine:
    return ChargeLine(
        key, quantity, unit_amount, rate_amount(quantity, unit_amount, currency), metadata or {}
    )


def _fixed_line(
    key: str, amount: Money, proration: Proration | None, metadata: Mapping[str, str] | None = None
) -> ChargeLine:
    meta = dict(metadata or {})
    charged = amount
    if proration is not None and not proration.is_full:
        charged = proration.apply(amount)
        meta["proration"] = str(proration)
    return ChargeLine(key, Decimal(1), amount.to_decimal(), charged, meta)


def _flat_fee_line(prefix: str, index: int, tier: Tier) -> list[ChargeLine]:
    if tier.flat_fee is None or tier.flat_fee.is_zero():
        return []
    return [_fixed_line(f"{prefix}.tier_flat_fee", tier.flat_fee, None, {"tier": str(index)})]


def _bound(value: Decimal | None) -> str:
    return "inf" if value is None else str(value)


def _per_unit(model: PerUnit, quantity: Decimal) -> list[ChargeLine]:
    return [_line("pricing.per_unit", quantity, model.unit_price, model.currency)]


def _package(model: Package, quantity: Decimal) -> list[ChargeLine]:
    blocks = Fraction(quantity) / model.package_size
    packages = blocks.numerator // blocks.denominator
    if model.rounding is PackageRounding.UP and blocks.denominator != 1:
        packages += 1
    amount = Money(model.package_price.minor_units * packages, model.currency)
    metadata = {
        "units": str(quantity),
        "package_size": str(model.package_size),
        "rounding": model.rounding.value,
    }
    return [
        ChargeLine(
            "pricing.package", Decimal(packages), model.package_price.to_decimal(), amount, metadata
        )
    ]


def _graduated(model: Graduated, quantity: Decimal) -> list[ChargeLine]:
    lines: list[ChargeLine] = []
    lower = Decimal(0)
    for index, tier in enumerate(model.tiers, start=1):
        if index > 1 and quantity <= lower:
            break
        top = quantity if tier.up_to is None else min(quantity, tier.up_to)
        units = max(EXACT.subtract(top, lower), Decimal(0))
        metadata = {"tier": str(index), "from": str(lower), "up_to": _bound(tier.up_to)}
        lines.append(
            _line("pricing.graduated.tier", units, tier.unit_price, model.currency, metadata)
        )
        if units > 0:
            lines.extend(_flat_fee_line("pricing.graduated", index, tier))
        if tier.up_to is not None:
            lower = tier.up_to
    return lines


def _volume(model: Volume, quantity: Decimal) -> list[ChargeLine]:
    lower = Decimal(0)
    for index, tier in enumerate(model.tiers, start=1):
        if tier.up_to is None or quantity <= tier.up_to:
            metadata = {"tier": str(index), "from": str(lower), "up_to": _bound(tier.up_to)}
            lines = [_line("pricing.volume", quantity, tier.unit_price, model.currency, metadata)]
            if quantity > 0:
                lines.extend(_flat_fee_line("pricing.volume", index, tier))
            return lines
        lower = tier.up_to
    raise AssertionError("unreachable: the last tier is open-ended")  # pragma: no cover


def _percentage(model: Percentage, base: Money) -> list[ChargeLine]:
    rate = Decimal(model.rate_bps).scaleb(-4, EXACT)
    uncapped = rate_amount(base.to_decimal(), rate, model.currency)
    amount = uncapped
    metadata = {"rate_bps": str(model.rate_bps)}
    if base.is_positive() and model.minimum is not None and amount < model.minimum:
        amount, metadata["cap"] = model.minimum, "minimum"
    if model.maximum is not None and amount > model.maximum:
        amount, metadata["cap"] = model.maximum, "maximum"
    if amount != uncapped:
        metadata["uncapped_minor_units"] = str(uncapped.minor_units)
    return [ChargeLine("pricing.percentage", base.to_decimal(), rate, amount, metadata)]


def _base_fee_with_overage(
    model: BaseFeeWithOverage, quantity: Decimal, proration: Proration | None
) -> list[ChargeLine]:
    overage = max(EXACT.subtract(quantity, model.included_units), Decimal(0))
    metadata = {"included_units": str(model.included_units), "units": str(quantity)}
    return [
        _fixed_line("pricing.base_fee", model.base_fee, proration),
        _line("pricing.overage", overage, model.overage_unit_price, model.currency, metadata),
    ]


def _usage_lines(
    model: UsageModel, usage: Decimal | Money, proration: Proration | None
) -> list[ChargeLine]:
    if isinstance(model, Percentage):
        if not isinstance(usage, Money):
            raise ValidationFailed("percentage prices rate a Money amount, not a quantity")
        if usage.currency != model.currency:
            raise ValidationFailed(
                f"amount is in {usage.currency.code}, price is in {model.currency.code}"
            )
        if usage.is_negative():
            raise ValidationFailed("rated amount must be >= 0")
        return _percentage(model, usage)
    if isinstance(model, FlatFee):
        return [_fixed_line("pricing.flat_fee", model.amount, proration)]
    if isinstance(usage, Money):
        raise ValidationFailed(f"{model.kind} prices rate a quantity, not a Money amount")
    if isinstance(model, PerUnit):
        return _per_unit(model, usage)
    if isinstance(model, Package):
        return _package(model, usage)
    if isinstance(model, Graduated):
        return _graduated(model, usage)
    if isinstance(model, Volume):
        return _volume(model, usage)
    return _base_fee_with_overage(model, usage, proration)


def price_lines(
    model: PriceModel, usage: Decimal | int | Money, *, proration: Proration | None = None
) -> tuple[ChargeLine, ...]:
    """Charge lines of ``model`` for ``usage``. Pure; the building block of ``rate``."""
    value: Decimal | Money = usage if isinstance(usage, Money) else check_quantity(usage)
    if not isinstance(model, MinimumCommitment):
        return tuple(_usage_lines(model, value, proration))
    lines = _usage_lines(model.inner, value, proration)
    subtotal = sum((line.amount for line in lines), Money.zero(model.currency))
    minimum = model.minimum if proration is None else proration.apply(model.minimum)
    if subtotal < minimum:
        metadata = {
            "minimum_minor_units": str(minimum.minor_units),
            "usage_minor_units": str(subtotal.minor_units),
        }
        if proration is not None and not proration.is_full:
            metadata["proration"] = str(proration)
        shortfall = minimum - subtotal
        lines.append(
            ChargeLine(
                "pricing.minimum_commitment.true_up",
                Decimal(1),
                shortfall.to_decimal(),
                shortfall,
                metadata,
            )
        )
    return tuple(lines)


def rate(
    version: PriceVersion,
    usage: Usage,
    period: BillingPeriod,
    *,
    proration: Proration | None = None,
) -> RatedCharge:
    """Rate ``usage`` for ``period`` with ``version``, which must be effective during the period.

    ``usage`` is a quantity (``Decimal``/``int``), an ``AggregatedUsage`` (pins the metric
    definition on the result) or, for percentage prices, a ``Money`` amount.
    """
    if not version.overlaps(period.start, period.end):
        raise ValidationFailed(
            f"price version {version.version} is not effective during the billing period"
        )
    metric_code: str | None = None
    metric_version: int | None = None
    value: Decimal | int | Money
    if isinstance(usage, AggregatedUsage):
        value, metric_code, metric_version = (
            usage.quantity,
            usage.metric_code,
            usage.metric_version,
        )
    else:
        value = usage
    lines = price_lines(version.model, value, proration=proration)
    currency = version.currency
    return RatedCharge(
        price_id=version.price_id,
        price_version_id=version.id,
        price_version=version.version,
        model=version.model.kind,
        period=period,
        currency=currency,
        lines=lines,
        total=sum((line.amount for line in lines), Money.zero(currency)),
        metric_code=metric_code,
        metric_version=metric_version,
    )
