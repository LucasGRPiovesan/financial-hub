"""Pure event-log domain: cursors, filters, retry maths, schedule maths, names."""

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest

from financial_hub.modules.events.domain import (
    ClaimedDelivery,
    Event,
    EventCursor,
    RetryPolicy,
    due_runs,
    is_valid_type_filter,
    matches,
    require_name,
    truncate_error,
)
from financial_hub.shared.ids import new_uuid
from financial_hub.shared.pagination import InvalidCursorError

AT = datetime(2026, 10, 10, 12, tzinfo=UTC)


def encode(value: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()


def make_event(**overrides: object) -> Event:
    values: dict[str, object] = {
        "id": new_uuid(),
        "type": "system.test",
        "version": 1,
        "project_id": new_uuid(),
        "subject": None,
        "occurred_at": AT,
        "correlation_id": None,
        "causation_id": None,
        "traceparent": None,
        "data": {},
        "txid": 42,
    }
    values.update(overrides)
    return Event(**values)  # pyright: ignore[reportArgumentType]


def test_cursor_round_trips_and_orders_by_txid_then_id() -> None:
    cursor = EventCursor(2**63 - 1, new_uuid())
    encoded = cursor.encode()
    assert "=" not in encoded
    assert EventCursor.decode(encoded) == cursor
    low, high = sorted([new_uuid(), new_uuid()])
    assert EventCursor(1, high) < EventCursor(2, low)
    assert EventCursor(1, low) < EventCursor(1, high)


@pytest.mark.parametrize(
    "value",
    [
        "%%%",
        base64.urlsafe_b64encode(b"\xff\xfe").decode(),
        encode("not a list"),
        encode([1]),
        encode([1, "x", 3]),
        encode([-1, str(new_uuid())]),
        encode([True, str(new_uuid())]),
        encode(["1", str(new_uuid())]),
        encode([1, 2]),
        encode([1, "not-a-uuid"]),
        "",
    ],
)
def test_malformed_cursors_are_rejected(value: str) -> None:
    with pytest.raises(InvalidCursorError):
        EventCursor.decode(value)


def test_event_exposes_public_id_and_cursor() -> None:
    event = make_event()
    assert event.public_id.startswith("evt_")
    assert event.cursor == EventCursor(42, event.id)
    claimed = ClaimedDelivery(new_uuid(), event, new_uuid(), "h", new_uuid(), AT, 1)
    assert claimed.event is event


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        (["*"], True),
        (["system.test"], True),
        (["system.*"], True),
        (["system.test.*"], False),
        (["system.tes"], False),
        (["sys.*"], False),
        (["ledger.*", "system.test"], True),
        ([], False),
    ],
)
def test_matches_mirrors_the_sql_predicate(filters: list[str], expected: bool) -> None:
    assert matches("system.test", filters) is expected


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("*", True),
        ("system", True),
        ("system.test", True),
        ("system.*", True),
        ("system.", False),
        ("*.test", False),
        ("System.test", False),
        ("system.**", False),
        ("", False),
    ],
)
def test_type_filter_syntax(value: str, valid: bool) -> None:
    assert is_valid_type_filter(value) is valid


def test_retry_policy_ceiling_grows_exponentially_up_to_the_cap() -> None:
    policy = RetryPolicy(base_seconds=2, cap_seconds=60, max_attempts=4)
    assert [policy.ceiling(n).total_seconds() for n in range(0, 7)] == [2, 2, 4, 8, 16, 32, 60]
    assert policy.ceiling(10_000) == timedelta(seconds=60)  # no overflow
    assert policy.delay(3, lambda: 0.5) == timedelta(seconds=4)
    assert policy.delay(3, lambda: -1.0) == timedelta(0)
    assert policy.delay(3, lambda: 7.0) == timedelta(seconds=8)
    assert not policy.exhausted(3)
    assert policy.exhausted(4)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"base_seconds": 0},
        {"base_seconds": 10, "cap_seconds": 5},
        {"max_attempts": 0},
    ],
)
def test_retry_policy_rejects_nonsense(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError, match="retry policy"):
        RetryPolicy(**kwargs)  # pyright: ignore[reportArgumentType]


def test_due_runs_lists_every_due_instant_up_to_the_limit() -> None:
    minute = timedelta(minutes=1)
    assert due_runs(AT, minute, AT - minute, 10) == ([], AT)
    assert due_runs(AT, minute, AT, 10) == ([AT], AT + minute)
    runs, following = due_runs(AT, minute, AT + 5 * minute, 3)
    assert runs == [AT, AT + minute, AT + 2 * minute]
    assert following == AT + 3 * minute
    with pytest.raises(ValueError, match="positive"):
        due_runs(AT, timedelta(0), AT, 1)


def test_truncate_error_and_require_name() -> None:
    assert truncate_error("short") == "short"
    long = truncate_error("x" * 5000)
    assert len(long) == 2000
    assert long.endswith("...")
    assert require_name("handler", "ledger.projector:v1") == "ledger.projector:v1"
    for bad in ("", "Upper", "1st", "with space", "x" * 129):
        with pytest.raises(ValueError, match="invalid handler name"):
            require_name("handler", bad)
