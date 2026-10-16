"""Metering domain (pure): exact measures, the accepted window, payload comparison, metrics."""

import re
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Float

from financial_hub.modules.metering.domain import (
    FUTURE_SKEW,
    PAST_WINDOW,
    Aggregation,
    BillableMetric,
    MetricStatus,
    UsageEvent,
    check_window,
    measure_of,
    parse_decimal,
    plain_decimal,
    same_payload,
    validate_code,
    validate_definition,
    validate_properties,
    validate_transaction_id,
)
from financial_hub.modules.metering.infrastructure.tables import billable_metrics, usage_events

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


def metric(aggregation: Aggregation, field: str | None) -> BillableMetric:
    return BillableMetric(
        id=uuid4(),
        project_id=uuid4(),
        code="m",
        name="M",
        description=None,
        aggregation=aggregation,
        field=field,
        status=MetricStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
        archived_at=None,
    )


# -- exact decimals ----------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0, "0"),
        (7, "7"),
        (-3, "-3"),
        ("12", "12"),
        ("0.1", "0.1"),
        ("0.000000001", "0.000000001"),
        ("-12.34567890", "-12.3456789"),
        ("123456789012345678901.123456789", "123456789012345678901.123456789"),
        ("1E+2", "100"),
    ],
)
def test_parse_decimal_is_exact(raw: object, expected: str) -> None:
    parsed = parse_decimal(raw)
    assert parsed == Decimal(expected)


def test_parse_decimal_keeps_what_binary_floats_would_lose() -> None:
    total = sum((parse_decimal("0.1") for _ in range(10)), Decimal(0))
    assert total == Decimal(1)
    assert 0.1 + 0.2 != 0.3  # the reason floats are refused


@pytest.mark.parametrize(
    "raw",
    [
        0.1,
        1.0,
        True,
        None,
        [],
        {},
        "abc",
        "",
        "NaN",
        "Infinity",
        "0.0000000001",  # 10 decimal places
        "1" * 22,  # 22 integer digits
        "1" * 70,
    ],
)
def test_parse_decimal_rejects_inexact_or_out_of_range(raw: object) -> None:
    with pytest.raises(ValueError):
        parse_decimal(raw)


def test_plain_decimal_never_uses_exponents_or_trailing_zeros() -> None:
    assert plain_decimal(Decimal("1E+2")) == "100"
    assert plain_decimal(Decimal("1.500000000")) == "1.5"
    assert plain_decimal(Decimal("-0.000000000")) == "0"
    assert plain_decimal(Decimal("0E-9")) == "0"


# -- what an event contributes to its metric ----------


def test_count_needs_no_property() -> None:
    measure = measure_of(metric(Aggregation.COUNT, None), {"anything": 1})
    assert measure.value is None
    assert measure.unique_value is None


def test_sum_and_max_read_the_field_as_an_exact_decimal() -> None:
    for aggregation in (Aggregation.SUM, Aggregation.MAX):
        measure = measure_of(metric(aggregation, "gb"), {"gb": "1.25"})
        assert measure.value == Decimal("1.25")
        assert measure.unique_value is None
    assert measure_of(metric(Aggregation.SUM, "gb"), {"gb": 3}).value == Decimal(3)


def test_sum_rejects_missing_float_and_malformed_fields() -> None:
    sum_gb = metric(Aggregation.SUM, "gb")
    for properties in ({}, {"gb": 1.5}, {"gb": "x"}, {"gb": None}, {"gb": True}):
        with pytest.raises(ValueError, match="gb"):
            measure_of(sum_gb, properties)


def test_unique_count_compares_scalars_as_text() -> None:
    unique = metric(Aggregation.UNIQUE_COUNT, "user")
    assert measure_of(unique, {"user": 7}).unique_value == "7"
    assert measure_of(unique, {"user": "7"}).unique_value == "7"
    for properties in ({}, {"user": ""}, {"user": 1.5}, {"user": True}, {"user": ["a"]}):
        with pytest.raises(ValueError, match="user"):
            measure_of(unique, properties)


# -- the accepted window: 30 days back, 5 minutes ahead ----------


def test_window_is_inclusive_at_both_edges() -> None:
    assert timedelta(days=30) == PAST_WINDOW
    assert timedelta(minutes=5) == FUTURE_SKEW
    assert check_window(NOW - PAST_WINDOW, NOW) == NOW - PAST_WINDOW
    assert check_window(NOW + FUTURE_SKEW, NOW) == NOW + FUTURE_SKEW
    assert check_window(NOW, NOW) == NOW


def test_window_rejects_just_outside() -> None:
    with pytest.raises(ValueError, match="30 days"):
        check_window(NOW - PAST_WINDOW - timedelta(seconds=1), NOW)
    with pytest.raises(ValueError, match="5 minutes"):
        check_window(NOW + FUTURE_SKEW + timedelta(seconds=1), NOW)


def test_window_needs_a_timezone_and_returns_utc() -> None:
    with pytest.raises(ValueError, match="timezone"):
        check_window(datetime(2026, 10, 16, 12, 0), NOW)
    brasilia = timezone(timedelta(hours=-3))
    local = datetime(2026, 10, 16, 9, 0, tzinfo=brasilia)
    assert check_window(local, NOW) == NOW
    assert check_window(local, NOW).utcoffset() == timedelta(0)


# -- idempotency: comparing a retry with the stored event ----------


def stored(**overrides: Any) -> UsageEvent:
    values: dict[str, Any] = {
        "id": uuid4(),
        "project_id": uuid4(),
        "customer_id": uuid4(),
        "metric_code": "api_calls",
        "transaction_id": "t-1",
        "occurred_at": NOW,
        "properties": {"gb": "1.5", "tags": ["a", "b"]},
        "value": Decimal("1.5"),
        "unique_value": None,
        "ingested_at": NOW,
    }
    values.update(overrides)
    return UsageEvent(**values)


def test_same_payload_ignores_key_order_and_an_omitted_timestamp() -> None:
    event = stored()
    assert same_payload(
        event,
        metric_code="api_calls",
        customer_id=event.customer_id,
        properties={"tags": ["a", "b"], "gb": "1.5"},
        timestamp=None,
    )
    assert same_payload(
        event,
        metric_code="api_calls",
        customer_id=event.customer_id,
        properties=event.properties,
        timestamp=NOW.astimezone(timezone(timedelta(hours=2))),
    )


@pytest.mark.parametrize(
    "change",
    [
        {"metric_code": "other"},
        {"customer_id": uuid4()},
        {"properties": {"gb": "1.5"}},
        {"properties": {"gb": "1.50", "tags": ["a", "b"]}},
        {"properties": {"gb": "1.5", "tags": ["b", "a"]}},
        {"timestamp": NOW + timedelta(seconds=1)},
    ],
)
def test_same_payload_detects_any_difference(change: dict[str, Any]) -> None:
    event = stored()
    arguments: dict[str, Any] = {
        "metric_code": "api_calls",
        "customer_id": event.customer_id,
        "properties": event.properties,
        "timestamp": NOW,
    }
    arguments.update(change)
    assert not same_payload(event, **arguments)


# -- shapes ----------


def test_transaction_ids() -> None:
    for good in ("a", "evt_1", "9f2:7@x+y=z~-.", "x" * 128):
        assert validate_transaction_id(good) == good
    for bad in ("", " a", "a b", "-a", "a/b", "é", "x" * 129):
        with pytest.raises(ValueError, match="transaction_id"):
            validate_transaction_id(bad)


def test_properties_must_be_finite_json_and_small() -> None:
    assert validate_properties({"a": [1, {"b": None}]}) == {"a": [1, {"b": None}]}
    with pytest.raises(ValueError, match="finite"):
        validate_properties({"a": float("nan")})
    with pytest.raises(ValueError, match="finite"):
        validate_properties({"a": object()})
    with pytest.raises(ValueError, match="exceed"):
        validate_properties({"a": "x" * 9000})


def test_metric_codes_and_definitions() -> None:
    assert validate_code("api_calls_2") == "api_calls_2"
    for bad in ("", "Api", "1a", "a-b", "a" * 64):
        with pytest.raises(ValueError, match="code"):
            validate_code(bad)
    assert validate_definition("count", None) == (Aggregation.COUNT, None)
    assert validate_definition("sum", "gb") == (Aggregation.SUM, "gb")
    with pytest.raises(ValueError, match="takes no field"):
        validate_definition("count", "gb")
    with pytest.raises(ValueError, match="needs the event property"):
        validate_definition("sum", None)
    with pytest.raises(ValueError, match="aggregation must be one of"):
        validate_definition("avg", "gb")
    with pytest.raises(ValueError, match="field must be"):
        validate_definition("max", "not valid")


# -- no floating point storage ----------


def test_no_float_columns_and_no_float_sql_types() -> None:
    for table in (billable_metrics, usage_events):
        assert not [c.name for c in table.columns if isinstance(c.type, Float)]
    migration = Path(__file__).parents[3] / "migrations" / "versions" / "0010_metering.py"
    sql = migration.read_text("utf-8").lower()
    sql = migration.read_text("utf-8").lower()
    assert not re.search(r"\b(double precision|real|float[48]?)\s*(,|\)|not null|default)", sql)
