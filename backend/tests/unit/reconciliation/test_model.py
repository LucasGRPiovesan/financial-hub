"""Windows, break transitions, reasons and the four-eyes payload hash (pure)."""

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from financial_hub.modules.reconciliation.application.service import day_bounds, provider_today
from financial_hub.modules.reconciliation.domain.model import (
    AUTO_RESOLVABLE,
    BreakKind,
    BreakState,
    IllegalBreakTransition,
    Window,
    adjustment_payload_hash,
    check_reason,
    check_transition,
)
from financial_hub.shared.errors import ValidationFailed


def test_a_window_is_inclusive_and_bounded() -> None:
    assert Window(date(2026, 10, 1), date(2026, 10, 31)).end == date(2026, 10, 31)
    with pytest.raises(ValidationFailed):
        Window(date(2026, 10, 2), date(2026, 10, 1))
    with pytest.raises(ValidationFailed):
        Window(date(2026, 10, 1), date(2026, 11, 1))  # 32 days


def test_the_default_window_is_yesterday() -> None:
    assert Window.day_before(date(2026, 10, 1)) == Window(date(2026, 9, 30), date(2026, 9, 30))


def test_days_follow_the_provider_calendar() -> None:
    # 01:30 UTC on Oct 2nd is still Oct 1st in Sao Paulo (UTC-3)
    assert provider_today(datetime(2026, 10, 2, 1, 30, tzinfo=UTC)) == date(2026, 10, 1)
    start, until = day_bounds(Window(date(2026, 10, 1), date(2026, 10, 1)))
    assert start.astimezone(UTC) == datetime(2026, 10, 1, 3, tzinfo=UTC)
    assert until.astimezone(UTC) == datetime(2026, 10, 2, 3, tzinfo=UTC)


def test_transitions_move_forward_only() -> None:
    assert check_transition(BreakState.OPEN, BreakState.RESOLVED, None) is None
    assert check_transition(BreakState.OPEN, BreakState.INVESTIGATING, " why ") == "why"
    assert check_transition(BreakState.INVESTIGATING, BreakState.ACCEPTED, "noise") == "noise"
    for closed in (BreakState.RESOLVED, BreakState.ACCEPTED):
        for target in BreakState:
            with pytest.raises(IllegalBreakTransition):
                check_transition(closed, target, "reason")
    with pytest.raises(IllegalBreakTransition):
        check_transition(BreakState.INVESTIGATING, BreakState.OPEN, "reason")


def test_investigating_and_accepted_need_a_reason() -> None:
    for target in (BreakState.INVESTIGATING, BreakState.ACCEPTED):
        with pytest.raises(ValidationFailed):
            check_transition(BreakState.OPEN, target, None)
        with pytest.raises(ValidationFailed):
            check_transition(BreakState.OPEN, target, "   ")
    with pytest.raises(ValidationFailed):
        check_reason("x" * 1001, required=False)
    assert check_reason("  ", required=False) is None


def test_only_status_and_fee_breaks_auto_resolve() -> None:
    assert {BreakKind.STATUS_MISMATCH, BreakKind.FEE_MISMATCH} == AUTO_RESOLVABLE


def test_the_payload_hash_pins_every_approved_field() -> None:
    base = {
        "break_id": uuid4(),
        "provider_account_id": uuid4(),
        "currency": "BRL",
        "amount": 100,
        "reason": "provider bonus",
    }
    digest = adjustment_payload_hash(**base)  # type: ignore[arg-type]

    assert len(digest) == 64 and int(digest, 16) >= 0
    assert adjustment_payload_hash(**base) == digest  # type: ignore[arg-type]
    for field, other in (
        ("break_id", uuid4()),
        ("provider_account_id", uuid4()),
        ("currency", "USD"),
        ("amount", -100),
        ("reason", "provider bonus!"),
    ):
        assert adjustment_payload_hash(**{**base, field: other}) != digest  # type: ignore[arg-type]
