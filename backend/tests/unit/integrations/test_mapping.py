"""Business event -> usage events, personal data redaction and the split allocation."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from financial_hub.modules.integrations.domain import (
    PLATFORM,
    Split,
    allocate_splits,
    check_idempotency_key,
    find_float,
    map_usage,
    parse_manifest,
    redact,
    validate_payload,
)
from financial_hub.shared.errors import ValidationFailed
from financial_hub.shared.money import BRL, Money
from tests.unit.integrations.samples import PAYLOAD, SAMPLE

FIELDS = {"api_calls": "calls", "consultations": None}


def drafts(payload: dict[str, Any], key: str = "order-1") -> Any:
    manifest = parse_manifest(SAMPLE)
    spec = manifest.business_events[0]
    return map_usage(manifest, spec, payload, idempotency_key=key, metric_fields=FIELDS)


def test_maps_one_usage_event_per_mapping() -> None:
    first, second = drafts({**PAYLOAD, "finished_at": "2026-10-01T12:00:00-03:00"})
    assert first.transaction_id == "order-1:api_calls"
    assert first.customer_external_ref == "cust-0001"
    assert first.properties == {"calls": "30"}
    assert first.timestamp == datetime(2026, 10, 1, 15, 0, tzinfo=UTC)
    assert second.transaction_id == "order-1:consultations"
    assert second.properties == {}
    assert second.timestamp is None


def test_a_retry_derives_the_same_usage_events() -> None:
    assert drafts(PAYLOAD) == drafts(PAYLOAD)


@pytest.mark.parametrize(
    ("payload", "pointer"),
    [
        ({**PAYLOAD, "customer": {}}, "/manifest/business_events/0/usage/0/customer"),
        ({**PAYLOAD, "minutes": True}, "/manifest/business_events/0/usage/0/quantity"),
        ({**PAYLOAD, "finished_at": "yesterday"}, "/manifest/business_events/0/usage/0/timestamp"),
        (
            {**PAYLOAD, "finished_at": "2026-10-01T12:00:00"},
            "/manifest/business_events/0/usage/0/timestamp",
        ),
    ],
)
def test_mapping_errors_point_into_the_manifest(payload: dict[str, Any], pointer: str) -> None:
    with pytest.raises(ValidationFailed) as caught:
        drafts(payload)
    assert caught.value.errors[0].pointer == pointer


def test_payload_validation() -> None:
    schema = parse_manifest(SAMPLE).business_events[0].payload_schema
    assert validate_payload(schema, PAYLOAD, "/payload") == []
    errors = validate_payload(schema, {"customer": {"id": 7}, "minutes": 0}, "/payload")
    found = {error.pointer for error in errors}
    assert {"/payload/customer/id", "/payload/minutes", "/payload/consultation_id"} <= found


def test_floats_are_found_anywhere() -> None:
    assert find_float({"a": [1, {"b": 2.0}]}) == "/a/1/b"
    assert find_float(PAYLOAD) is None


@pytest.mark.parametrize("key", ["", "-lead", "a" * 65, "has space", "ü"])
def test_idempotency_keys_are_bounded(key: str) -> None:
    with pytest.raises(ValidationFailed):
        check_idempotency_key(key)


def test_redaction_touches_only_declared_personal_data() -> None:
    payload = {**PAYLOAD, "contact": {"email": "other@example.com"}}
    stored = redact(parse_manifest(SAMPLE), payload)
    assert stored["customer"] == {"id": "cust-0001", "name": "[redacted]", "email": "[redacted]"}
    assert stored["contact"] == {"email": "[redacted]"}
    assert stored["minutes"] == 30
    assert payload["customer"]["name"] == "Maria Silva"  # the input is not mutated


def test_split_allocation_largest_remainder_platform_last() -> None:
    splits = [Split(payee="a", basis_points=3333), Split(payee="b", basis_points=3333)]
    parts = allocate_splits(Money(100, BRL), splits)
    assert [part.payee for part in parts] == ["a", "b", PLATFORM]
    assert [part.basis_points for part in parts] == [3333, 3333, 3334]
    assert sum(part.amount.minor_units for part in parts) == 100


@given(
    total=st.integers(min_value=0, max_value=10**12),
    weights=st.lists(st.integers(min_value=1, max_value=2500), max_size=4),
)
def test_split_parts_always_sum_to_the_total(total: int, weights: list[int]) -> None:
    splits = [Split(payee=f"p{i}", basis_points=bp) for i, bp in enumerate(weights)]
    parts = allocate_splits(Money(total, BRL), splits)
    assert sum(part.amount.minor_units for part in parts) == total
    assert parts[-1].payee == PLATFORM
    for part in parts:
        exact = Decimal(total) * part.basis_points / 10_000
        assert abs(Decimal(part.amount.minor_units) - exact) < 1
