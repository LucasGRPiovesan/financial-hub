"""Usage events (pure): the accepted time window, exact measures and payload comparison.

Measures are never floats. A ``sum``/``max`` property is an integer or a decimal *string* with at
most 9 decimal places and 21 integer digits (the ``numeric(30,9)`` column); a JSON float is
rejected because it is already inexact. A ``unique_count`` property is a string or an integer
(compared as text, so ``7`` and ``"7"`` are the same value).
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Context, Decimal, InvalidOperation
from typing import Any, Final
from uuid import UUID

from financial_hub.modules.metering.domain.metrics import Aggregation, BillableMetric

MAX_BATCH: Final = 100
PAST_WINDOW: Final = timedelta(days=30)
FUTURE_SKEW: Final = timedelta(minutes=5)
VALUE_SCALE: Final = 9
VALUE_INTEGER_DIGITS: Final = 21
PROPERTIES_MAX_BYTES: Final = 8192
UNIQUE_VALUE_MAX_LENGTH: Final = 256
TRANSACTION_ID_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+=~-]{0,127}$")

_QUANTUM: Final = Decimal(1).scaleb(-VALUE_SCALE)
_CONTEXT: Final = Context(prec=VALUE_INTEGER_DIGITS + VALUE_SCALE + 10)
_MAX_DECIMAL_TEXT: Final = 64


@dataclass(frozen=True, slots=True)
class Measure:
    """What an event contributes to its metric: ``value`` (sum/max), ``unique_value`` or neither
    (count)."""

    value: Decimal | None = None
    unique_value: str | None = None


@dataclass(frozen=True, slots=True)
class UsageEvent:
    id: UUID
    project_id: UUID
    customer_id: UUID
    metric_code: str
    transaction_id: str
    occurred_at: datetime
    properties: dict[str, Any]
    value: Decimal | None
    unique_value: str | None
    ingested_at: datetime


def validate_transaction_id(value: str) -> str:
    if TRANSACTION_ID_PATTERN.fullmatch(value) is None:
        raise ValueError(
            "transaction_id must be 1-128 characters: letters, digits and . _ : @ + = ~ -"
        )
    return value


def validate_properties(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except TypeError, ValueError:
        raise ValueError("properties must be a JSON object of finite values") from None
    if len(encoded.encode()) > PROPERTIES_MAX_BYTES:
        raise ValueError(f"properties must not exceed {PROPERTIES_MAX_BYTES} bytes")
    return dict(value)


def check_window(timestamp: datetime, now: datetime) -> datetime:
    """``timestamp`` in UTC if it is within ``[now - 30 days, now + 5 minutes]``."""
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("timestamp must include a timezone offset")
    moment = timestamp.astimezone(UTC)
    if moment < now - PAST_WINDOW:
        raise ValueError(
            f"timestamp is more than {PAST_WINDOW.days} days in the past; "
            "usage that old is not accepted"
        )
    if moment > now + FUTURE_SKEW:
        raise ValueError(
            f"timestamp is more than {int(FUTURE_SKEW.total_seconds() // 60)} minutes in the future"
        )
    return moment


def parse_decimal(raw: object) -> Decimal:
    """An exact ``numeric(30,9)`` value from an int or a decimal string; never from a float."""
    if isinstance(raw, (bool, float)) or not isinstance(raw, (int, str)):
        raise ValueError("must be an integer or a decimal string (floats are inexact)")
    if isinstance(raw, str) and len(raw) > _MAX_DECIMAL_TEXT:
        raise ValueError("number is too long")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise ValueError("not a valid number") from None
    if not value.is_finite() or value.adjusted() >= VALUE_INTEGER_DIGITS:
        raise ValueError(f"must have at most {VALUE_INTEGER_DIGITS} integer digits")
    try:
        exact = value.quantize(_QUANTUM, context=_CONTEXT)
    except InvalidOperation:  # pragma: no cover - precision above covers the whole range
        raise ValueError("not representable") from None
    if exact != value:
        raise ValueError(f"must have at most {VALUE_SCALE} decimal places")
    return exact


def measure_of(metric: BillableMetric, properties: Mapping[str, Any]) -> Measure:
    """The measure ``properties`` give ``metric``; ``ValueError`` names what is wrong."""
    if metric.aggregation is Aggregation.COUNT:
        return Measure()
    field = metric.field or ""
    if field not in properties:
        raise ValueError(f"properties.{field} is required by metric {metric.code}")
    raw = properties[field]
    if metric.aggregation is Aggregation.UNIQUE_COUNT:
        if isinstance(raw, bool) or not isinstance(raw, (int, str)):
            raise ValueError(f"properties.{field} must be a string or an integer")
        text = str(raw)
        if not 1 <= len(text) <= UNIQUE_VALUE_MAX_LENGTH:
            raise ValueError(
                f"properties.{field} must have 1..{UNIQUE_VALUE_MAX_LENGTH} characters"
            )
        return Measure(unique_value=text)
    try:
        return Measure(value=parse_decimal(raw))
    except ValueError as error:
        raise ValueError(f"properties.{field} {error}") from None


def same_payload(
    stored: UsageEvent,
    *,
    metric_code: str,
    customer_id: UUID,
    properties: Mapping[str, Any],
    timestamp: datetime | None,
) -> bool:
    """Whether a retry carries the stored event's payload (``timestamp`` only if the retry sent
    one: an omitted timestamp means "when you received it", which differs on every attempt)."""
    return (
        stored.metric_code == metric_code
        and stored.customer_id == customer_id
        and stored.properties == dict(properties)
        and (timestamp is None or stored.occurred_at == timestamp.astimezone(UTC))
    )


def plain_decimal(value: Decimal) -> str:
    """Positional notation without trailing zeros (``Decimal("1E+2")`` -> ``"100"``)."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text
