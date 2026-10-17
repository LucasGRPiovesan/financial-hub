"""Price model <-> JSON (the ``price_versions.model`` column and the HTTP body).

Format (``kind`` selects the model; the currency is the price's, never in the JSON):

- quantities and unit prices are **decimal strings** in major units (``"0.0015"``), so they stay
  exact through JSON; a JSON number is refused for them;
- fixed amounts are **integers in minor units** (``4990`` = BRL 49.90);
- ``{"kind": "per_unit", "unit_price": "0.0015"}``
- ``{"kind": "package", "package_size": 100, "package_price": 500, "rounding": "up"}``
- ``{"kind": "graduated" | "volume", "tiers": [{"up_to": "1000", "unit_price": "0",
  "flat_fee": 0}, {"up_to": null, "unit_price": "0.01"}]}``
- ``{"kind": "percentage", "rate_bps": 250, "minimum": 100, "maximum": null}``
- ``{"kind": "flat_fee", "amount": 4990}``
- ``{"kind": "base_fee_with_overage", "base_fee": 9900, "included_units": "1000",
  "overage_unit_price": "0.05"}``
- ``{"kind": "minimum_commitment", "minimum": 10000, "inner": {...a model above...}}``

Every value is validated by the pricing domain; errors carry JSON pointers under ``/model``.
"""

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any, cast

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
    Tier,
    UsageModel,
    Volume,
)
from financial_hub.shared.errors import FieldError, ValidationFailed
from financial_hub.shared.money import Currency, Money

ROOT = "/model"

_KEYS: dict[str, frozenset[str]] = {
    PerUnit.kind: frozenset({"unit_price"}),
    Package.kind: frozenset({"package_size", "package_price", "rounding"}),
    Graduated.kind: frozenset({"tiers"}),
    Volume.kind: frozenset({"tiers"}),
    Percentage.kind: frozenset({"rate_bps", "minimum", "maximum"}),
    FlatFee.kind: frozenset({"amount"}),
    BaseFeeWithOverage.kind: frozenset({"base_fee", "included_units", "overage_unit_price"}),
    MinimumCommitment.kind: frozenset({"minimum", "inner"}),
}
_TIER_KEYS = frozenset({"up_to", "unit_price", "flat_fee"})


def _invalid(pointer: str, detail: str) -> ValidationFailed:
    return ValidationFailed(
        "The price model is invalid.", errors=[FieldError(pointer=pointer, detail=detail)]
    )


def _plain(value: Decimal) -> str:
    return format(value.normalize(), "f") if value else "0"


def _minor(value: Money | None) -> int | None:
    return None if value is None else value.minor_units


def _tiers_json(tiers: tuple[Tier, ...]) -> list[dict[str, Any]]:
    return [
        {
            "up_to": None if tier.up_to is None else _plain(tier.up_to),
            "unit_price": _plain(tier.unit_price),
            "flat_fee": _minor(tier.flat_fee),
        }
        for tier in tiers
    ]


def model_to_json(model: PriceModel) -> dict[str, Any]:
    """The canonical JSON of ``model`` (round-trips through :func:`model_from_json`)."""
    match model:
        case PerUnit():
            return {"kind": model.kind, "unit_price": _plain(model.unit_price)}
        case Package():
            return {
                "kind": model.kind,
                "package_size": model.package_size,
                "package_price": model.package_price.minor_units,
                "rounding": model.rounding.value,
            }
        case Graduated() | Volume():
            return {"kind": model.kind, "tiers": _tiers_json(model.tiers)}
        case Percentage():
            return {
                "kind": model.kind,
                "rate_bps": model.rate_bps,
                "minimum": _minor(model.minimum),
                "maximum": _minor(model.maximum),
            }
        case FlatFee():
            return {"kind": model.kind, "amount": model.amount.minor_units}
        case BaseFeeWithOverage():
            return {
                "kind": model.kind,
                "base_fee": model.base_fee.minor_units,
                "included_units": _plain(model.included_units),
                "overage_unit_price": _plain(model.overage_unit_price),
            }
        case MinimumCommitment():
            return {
                "kind": model.kind,
                "minimum": model.minimum.minor_units,
                "inner": model_to_json(model.inner),
            }


class _Reader:
    def __init__(self, data: Mapping[str, Any], pointer: str, currency: Currency) -> None:
        self._data = data
        self._pointer = pointer
        self.currency = currency

    def at(self, key: str) -> str:
        return f"{self._pointer}/{key}"

    def raw(self, key: str) -> object:
        return self._data.get(key)

    def integer(self, key: str, *, required: bool = True) -> int | None:
        value = self._data.get(key)
        if value is None and not required:
            return None
        if type(value) is not int:
            raise _invalid(self.at(key), "must be an integer")
        return value

    def amount(self, key: str, *, required: bool = True) -> Money | None:
        value = self.integer(key, required=required)
        return None if value is None else Money(value, self.currency)

    def required_integer(self, key: str) -> int:
        value = self.integer(key)
        if value is None:  # unreachable: a required key is never None
            raise _invalid(self.at(key), "is required")
        return value

    def required_amount(self, key: str) -> Money:
        return Money(self.required_integer(key), self.currency)

    def decimal(self, key: str, *, required: bool = True) -> Decimal | None:
        value = self._data.get(key)
        if value is None and not required:
            return None
        if not isinstance(value, str):
            raise _invalid(self.at(key), 'must be a decimal string such as "0.0015"')
        try:
            number = Decimal(value.strip())
        except InvalidOperation:
            raise _invalid(self.at(key), f"not a decimal number: {value!r}") from None
        if not number.is_finite():
            raise _invalid(self.at(key), "must be finite")
        return number

    def required_decimal(self, key: str) -> Decimal:
        number = self.decimal(key)
        if number is None:  # unreachable: a required key is never None
            raise _invalid(self.at(key), "is required")
        return number


def _object(value: object, pointer: str, allowed: frozenset[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _invalid(pointer, "must be an object")
    data = cast(Mapping[str, Any], value)
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise _invalid(f"{pointer}/{unknown[0]}", "unknown field")
    return data


def _tiers(reader: _Reader) -> tuple[Tier, ...]:
    raw = reader.raw("tiers")
    if not isinstance(raw, list):
        raise _invalid(reader.at("tiers"), "must be a list of tiers")
    tiers: list[Tier] = []
    for index, item in enumerate(cast(list[object], raw)):
        pointer = reader.at(f"tiers/{index}")
        tier = _Reader(_object(item, pointer, _TIER_KEYS), pointer, reader.currency)
        tiers.append(
            Tier(
                up_to=tier.decimal("up_to", required=False),
                unit_price=tier.required_decimal("unit_price"),
                flat_fee=tier.amount("flat_fee", required=False),
            )
        )
    return tuple(tiers)


def _build(data: object, pointer: str, currency: Currency, *, nested: bool) -> PriceModel:
    if not isinstance(data, Mapping):
        raise _invalid(pointer, "must be an object")
    fields = cast(Mapping[str, Any], data)
    kind = fields.get("kind")
    if not isinstance(kind, str) or kind not in _KEYS:
        raise _invalid(f"{pointer}/kind", f"must be one of {', '.join(sorted(_KEYS))}")
    if nested and kind == MinimumCommitment.kind:
        raise _invalid(f"{pointer}/kind", "a minimum commitment cannot wrap another one")
    reader = _Reader(_object(fields, pointer, _KEYS[kind] | {"kind"}), pointer, currency)
    match kind:
        case PerUnit.kind:
            return PerUnit(currency, reader.required_decimal("unit_price"))
        case Package.kind:
            rounding = reader.raw("rounding") or PackageRounding.UP.value
            if rounding not in {item.value for item in PackageRounding}:
                raise _invalid(reader.at("rounding"), "must be up or down")
            size = reader.required_integer("package_size")
            return Package(
                currency,
                size,
                reader.required_amount("package_price"),
                PackageRounding(cast(str, rounding)),
            )
        case Graduated.kind:
            return Graduated(currency, _tiers(reader))
        case Volume.kind:
            return Volume(currency, _tiers(reader))
        case Percentage.kind:
            rate_bps = reader.required_integer("rate_bps")
            return Percentage(
                currency,
                rate_bps,
                reader.amount("minimum", required=False),
                reader.amount("maximum", required=False),
            )
        case FlatFee.kind:
            return FlatFee(currency, reader.required_amount("amount"))
        case BaseFeeWithOverage.kind:
            return BaseFeeWithOverage(
                currency,
                reader.required_amount("base_fee"),
                reader.required_decimal("included_units"),
                reader.required_decimal("overage_unit_price"),
            )
        case _:
            inner = _build(reader.raw("inner"), reader.at("inner"), currency, nested=True)
            return MinimumCommitment(
                currency, reader.required_amount("minimum"), cast(UsageModel, inner)
            )


def model_from_json(data: object, currency: Currency) -> PriceModel:
    """Parse and validate a model; any problem is a ``ValidationFailed`` pointing into it."""
    try:
        return _build(data, ROOT, currency, nested=False)
    except ValidationFailed as error:
        if error.errors and all(e.pointer.startswith(ROOT) for e in error.errors):
            raise
        detail = error.errors[0].detail if error.errors else str(error)
        pointer = error.errors[0].pointer if error.errors else ""
        raise _invalid(f"{ROOT}{pointer}", detail) from None
    except TypeError as error:
        raise _invalid(ROOT, str(error)) from None
