"""Orthogonal payment state: exhaustive transition tables and the public status precedence."""

from datetime import date, timedelta
from itertools import product

import pytest
from hypothesis import given
from hypothesis import strategies as st

from financial_hub.modules.payments.domain import (
    CaptureStatus,
    CreationStatus,
    DisputeStatus,
    IllegalTransition,
    PaymentState,
    PaymentStatus,
    RefundStatus,
    SettlementStatus,
    StaleTransition,
    move_capture,
    move_dispute,
    move_settlement,
    public_status,
    refund_status,
)

C, S, D = CaptureStatus, SettlementStatus, DisputeStatus

# (current, target) -> "ok" | "stale" | "illegal"; every pair is listed (exhaustive).
CAPTURE_TABLE: dict[tuple[CaptureStatus, CaptureStatus], str] = {
    (C.PENDING, C.PENDING): "ok",
    (C.PENDING, C.CAPTURED): "ok",
    (C.PENDING, C.FAILED): "ok",
    (C.PENDING, C.CANCELED): "ok",
    (C.CAPTURED, C.PENDING): "stale",
    (C.CAPTURED, C.CAPTURED): "ok",
    (C.CAPTURED, C.FAILED): "stale",
    (C.CAPTURED, C.CANCELED): "stale",
    (C.FAILED, C.PENDING): "stale",
    (C.FAILED, C.CAPTURED): "ok",  # a late boleto payment (R2b)
    (C.FAILED, C.FAILED): "ok",
    (C.FAILED, C.CANCELED): "illegal",
    (C.CANCELED, C.PENDING): "stale",
    (C.CANCELED, C.CAPTURED): "ok",
    (C.CANCELED, C.FAILED): "illegal",
    (C.CANCELED, C.CANCELED): "ok",
}
SETTLEMENT_TABLE: dict[tuple[SettlementStatus, SettlementStatus], str] = {
    (S.NONE, S.NONE): "ok",
    (S.NONE, S.SETTLED): "ok",
    (S.SETTLED, S.NONE): "stale",
    (S.SETTLED, S.SETTLED): "ok",
}
DISPUTE_TABLE: dict[tuple[DisputeStatus, DisputeStatus], str] = {
    (D.NONE, D.NONE): "ok",
    (D.NONE, D.OPEN): "ok",
    (D.NONE, D.WON): "illegal",
    (D.NONE, D.LOST): "illegal",
    (D.OPEN, D.NONE): "stale",
    (D.OPEN, D.OPEN): "ok",
    (D.OPEN, D.WON): "ok",
    (D.OPEN, D.LOST): "ok",
    (D.WON, D.NONE): "stale",
    (D.WON, D.OPEN): "ok",  # reopened (awaiting chargeback reversal)
    (D.WON, D.WON): "ok",
    (D.WON, D.LOST): "illegal",
    (D.LOST, D.NONE): "stale",
    (D.LOST, D.OPEN): "ok",
    (D.LOST, D.WON): "illegal",
    (D.LOST, D.LOST): "ok",
}


def test_the_tables_are_exhaustive() -> None:
    assert set(CAPTURE_TABLE) == set(product(C, C))
    assert set(SETTLEMENT_TABLE) == set(product(S, S))
    assert set(DISPUTE_TABLE) == set(product(D, D))


def captured(
    *, settlement_status: SettlementStatus = S.NONE, dispute_status: DisputeStatus = D.NONE
) -> PaymentState:
    return PaymentState(
        capture_status=C.CAPTURED,
        captured_amount=1000,
        settlement_status=settlement_status,
        dispute_status=dispute_status,
    )


def expect(outcome: str, call: object) -> None:
    assert callable(call)
    if outcome == "ok":
        call()
    elif outcome == "stale":
        with pytest.raises(StaleTransition):
            call()
    else:
        with pytest.raises(IllegalTransition) as raised:
            call()
        assert not isinstance(raised.value, StaleTransition)


@pytest.mark.parametrize(("pair", "outcome"), CAPTURE_TABLE.items())
def test_capture_transitions(pair: tuple[CaptureStatus, CaptureStatus], outcome: str) -> None:
    current, target = pair
    state = captured() if current is C.CAPTURED else PaymentState(capture_status=current)

    expect(outcome, lambda: move_capture(state, target, amount=1000, net_amount=950))
    if outcome == "ok":
        moved = move_capture(state, target, amount=1000, net_amount=950)
        assert moved.capture_status is target
        if target is C.CAPTURED and current is not C.CAPTURED:
            assert (moved.captured_amount, moved.net_amount, moved.fee_estimated) == (1000, 950, 50)


@pytest.mark.parametrize(("pair", "outcome"), SETTLEMENT_TABLE.items())
def test_settlement_transitions(
    pair: tuple[SettlementStatus, SettlementStatus], outcome: str
) -> None:
    current, target = pair
    state = captured(settlement_status=current)

    expect(outcome, lambda: move_settlement(state, target))
    if outcome == "ok":
        assert move_settlement(state, target).settlement_status is target


@pytest.mark.parametrize(("pair", "outcome"), DISPUTE_TABLE.items())
def test_dispute_transitions(pair: tuple[DisputeStatus, DisputeStatus], outcome: str) -> None:
    current, target = pair
    state = captured(dispute_status=current)

    expect(outcome, lambda: move_dispute(state, target))
    if outcome == "ok":
        assert move_dispute(state, target).dispute_status is target


@pytest.mark.parametrize("capture", [C.PENDING, C.FAILED, C.CANCELED])
def test_settling_or_disputing_requires_a_capture(capture: CaptureStatus) -> None:
    state = PaymentState(capture_status=capture)

    with pytest.raises(IllegalTransition, match="requires capture_status captured"):
        move_settlement(state, S.SETTLED)
    with pytest.raises(IllegalTransition, match="requires capture_status captured"):
        move_dispute(state, D.OPEN)
    assert move_settlement(state, S.NONE) == state
    assert move_dispute(state, D.NONE) == state


def test_a_repeated_capture_keeps_the_first_amounts() -> None:
    state = move_capture(PaymentState(), C.CAPTURED, amount=1000, net_amount=900)

    assert move_capture(state, C.CAPTURED, amount=5, net_amount=1) is state


@pytest.mark.parametrize(
    ("amount", "net"),
    [(0, None), (-1, None), (1000, 1001), (1000, -1)],
)
def test_a_capture_needs_coherent_int_amounts(amount: int, net: int | None) -> None:
    with pytest.raises(ValueError):
        move_capture(PaymentState(), C.CAPTURED, amount=amount, net_amount=net)


@pytest.mark.parametrize("amount", [10.0, "10", True])
def test_money_is_never_a_float_in_the_state(amount: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        move_capture(PaymentState(), C.CAPTURED, amount=amount)  # pyright: ignore[reportArgumentType]
    with pytest.raises(TypeError):
        PaymentState(captured_amount=amount)  # pyright: ignore[reportArgumentType]
    with pytest.raises(TypeError):
        PaymentState(net_amount=1.5)  # pyright: ignore[reportArgumentType]


def test_a_negative_captured_amount_is_refused() -> None:
    with pytest.raises(ValueError):
        PaymentState(captured_amount=-1)


@given(
    st.lists(
        st.one_of(st.sampled_from(list(C)), st.sampled_from(list(S)), st.sampled_from(list(D))),
        max_size=12,
    )
)
def test_any_sequence_of_moves_only_goes_forward(targets: list[object]) -> None:
    """Property: whatever arrives (in any order), each field ends at a value it may reach and
    a move never lowers a field's rank, and an accepted move replayed is a no-op."""
    rank = {C.PENDING: 0, C.FAILED: 1, C.CANCELED: 1, C.CAPTURED: 2}
    state = PaymentState()
    for target in targets:
        before = state
        try:
            if isinstance(target, CaptureStatus):
                state = move_capture(state, target, amount=500)
            elif isinstance(target, SettlementStatus):
                state = move_settlement(state, target)
            else:
                assert isinstance(target, DisputeStatus)
                state = move_dispute(state, target)
        except IllegalTransition:
            assert state == before
            continue
        assert rank[state.capture_status] >= rank[before.capture_status]
        assert not (before.settlement_status is S.SETTLED and state.settlement_status is S.NONE)
        if state.capture_status is not C.CAPTURED:
            assert state.settlement_status is S.NONE and state.dispute_status is D.NONE
        if isinstance(target, CaptureStatus):
            assert move_capture(state, target, amount=500) == state


# --- precedence --------------------------------------------------------------------------------

DUE = date(2026, 10, 15)
BEFORE, AFTER = DUE - timedelta(days=1), DUE + timedelta(days=1)

# (creation, state, refund, today) -> status, one row per precedence rule (highest first).
PRECEDENCE: list[tuple[CreationStatus, PaymentState | None, RefundStatus, date, PaymentStatus]] = [
    (
        CreationStatus.CREATED,
        captured(dispute_status=D.OPEN),
        RefundStatus.FULL,
        AFTER,
        PaymentStatus.DISPUTED,
    ),
    (
        CreationStatus.CREATED,
        captured(dispute_status=D.LOST),
        RefundStatus.PARTIAL,
        AFTER,
        PaymentStatus.CHARGED_BACK,
    ),
    (
        CreationStatus.CREATED,
        captured(settlement_status=S.SETTLED),
        RefundStatus.FULL,
        AFTER,
        PaymentStatus.REFUNDED,
    ),
    (
        CreationStatus.CREATED,
        captured(settlement_status=S.SETTLED),
        RefundStatus.PARTIAL,
        AFTER,
        PaymentStatus.PARTIALLY_REFUNDED,
    ),
    (
        CreationStatus.CREATED,
        captured(dispute_status=D.WON, settlement_status=S.SETTLED),
        RefundStatus.NONE,
        AFTER,
        PaymentStatus.SETTLED,
    ),
    (
        CreationStatus.CREATED,
        captured(dispute_status=D.WON),
        RefundStatus.NONE,
        AFTER,
        PaymentStatus.SUCCEEDED,
    ),
    (CreationStatus.CREATED, PaymentState(), RefundStatus.NONE, AFTER, PaymentStatus.OVERDUE),
    (CreationStatus.UNKNOWN, None, RefundStatus.NONE, AFTER, PaymentStatus.OVERDUE),
    (
        CreationStatus.CREATED,
        PaymentState(capture_status=C.FAILED),
        RefundStatus.NONE,
        AFTER,
        PaymentStatus.FAILED,
    ),
    (CreationStatus.REJECTED, None, RefundStatus.NONE, AFTER, PaymentStatus.FAILED),
    (CreationStatus.REJECTED, None, RefundStatus.NONE, BEFORE, PaymentStatus.FAILED),
    (
        CreationStatus.CREATED,
        PaymentState(capture_status=C.CANCELED),
        RefundStatus.NONE,
        AFTER,
        PaymentStatus.CANCELED,
    ),
    (CreationStatus.CREATE_REQUESTED, None, RefundStatus.NONE, DUE, PaymentStatus.PROCESSING),
    (CreationStatus.UNKNOWN, None, RefundStatus.NONE, BEFORE, PaymentStatus.PROCESSING),
    (
        CreationStatus.CREATED,
        PaymentState(),
        RefundStatus.NONE,
        DUE,
        PaymentStatus.REQUIRES_PAYMENT,
    ),
]


@pytest.mark.parametrize(("creation", "state", "refund", "today", "expected"), PRECEDENCE)
def test_public_status_precedence(
    creation: CreationStatus,
    state: PaymentState | None,
    refund: RefundStatus,
    today: date,
    expected: PaymentStatus,
) -> None:
    status = public_status(creation=creation, state=state, refund=refund, due_date=DUE, today=today)

    assert status is expected


def test_every_public_status_is_reachable() -> None:
    assert {row[-1] for row in PRECEDENCE} == set(PaymentStatus)


@given(
    st.sampled_from(list(CreationStatus)),
    st.sampled_from(list(C)),
    st.sampled_from(list(S)),
    st.sampled_from(list(D)),
    st.sampled_from(list(RefundStatus)),
    st.integers(min_value=-3, max_value=3),
)
def test_public_status_is_total_and_disputes_win(
    creation: CreationStatus,
    capture: CaptureStatus,
    settlement: SettlementStatus,
    dispute: DisputeStatus,
    refund: RefundStatus,
    offset: int,
) -> None:
    state = PaymentState(
        capture_status=capture, settlement_status=settlement, dispute_status=dispute
    )

    status = public_status(
        creation=creation,
        state=state,
        refund=refund,
        due_date=DUE,
        today=DUE + timedelta(days=offset),
    )

    assert isinstance(status, PaymentStatus)
    if dispute is D.OPEN:
        assert status is PaymentStatus.DISPUTED
    elif dispute is D.LOST:
        assert status is PaymentStatus.CHARGED_BACK
    elif refund is RefundStatus.FULL:
        assert status is PaymentStatus.REFUNDED


@pytest.mark.parametrize(
    ("captured_amount", "refunded", "expected"),
    [
        (1000, 0, RefundStatus.NONE),
        (1000, 1, RefundStatus.PARTIAL),
        (1000, 999, RefundStatus.PARTIAL),
        (1000, 1000, RefundStatus.FULL),
        (0, 0, RefundStatus.NONE),
    ],
)
def test_refund_status_is_derived_from_done_refunds(
    captured_amount: int, refunded: int, expected: RefundStatus
) -> None:
    assert refund_status(captured_amount, refunded) is expected
