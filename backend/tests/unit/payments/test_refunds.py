"""Refund states and the refundable amount: never above what was captured."""

from itertools import product

import pytest
from hypothesis import given
from hypothesis import strategies as st

from financial_hub.modules.payments.domain import (
    IllegalTransition,
    RefundExceedsRefundable,
    RefundState,
    StaleTransition,
    check_refund,
    move_refund,
    refundable_amount,
    refunded_amount,
)

R = RefundState
ALLOWED = {
    R.REQUESTED: {R.SUBMITTED, R.DONE, R.DENIED, R.UNKNOWN},
    R.SUBMITTED: {R.DONE, R.DENIED, R.CANCELLED, R.UNKNOWN},
    R.UNKNOWN: {R.SUBMITTED, R.DONE, R.DENIED, R.REQUESTED},
    R.DONE: set[RefundState](),
    R.DENIED: set[RefundState](),
    R.CANCELLED: set[RefundState](),
}
RANK = {R.REQUESTED: 0, R.UNKNOWN: 1, R.SUBMITTED: 2, R.DONE: 3, R.DENIED: 3, R.CANCELLED: 3}


@pytest.mark.parametrize(("current", "target"), list(product(R, R)))
def test_every_refund_transition(current: RefundState, target: RefundState) -> None:
    if target is current or target in ALLOWED[current]:
        assert move_refund(current, target) is target
    elif RANK[target] < RANK[current]:
        with pytest.raises(StaleTransition):
            move_refund(current, target)
    else:
        with pytest.raises(IllegalTransition) as raised:
            move_refund(current, target)
        assert not isinstance(raised.value, StaleTransition)


def test_partial_refunds_sum_and_failed_ones_release_the_amount() -> None:
    refunds = [(R.DONE, 300), (R.SUBMITTED, 200), (R.DENIED, 400), (R.CANCELLED, 100)]

    assert refunded_amount(refunds) == 300
    assert refundable_amount(1000, refunds) == 500
    assert check_refund(500, 1000, refunds) == 500
    with pytest.raises(RefundExceedsRefundable) as raised:
        check_refund(501, 1000, refunds)
    assert (raised.value.amount, raised.value.refundable) == (501, 500)


def test_unknown_and_requested_refunds_hold_their_amount() -> None:
    assert refundable_amount(1000, [(R.UNKNOWN, 600), (R.REQUESTED, 400)]) == 0


@pytest.mark.parametrize("amount", [0, -1, 1.5, "10", True])
def test_a_refund_amount_is_a_positive_int(amount: object) -> None:
    with pytest.raises(ValueError):
        check_refund(amount, 1000, [])  # pyright: ignore[reportArgumentType]


@given(
    st.integers(min_value=1, max_value=10**9),
    st.lists(
        st.tuples(st.sampled_from(list(R)), st.integers(min_value=1, max_value=10**6)),
        max_size=10,
    ),
    st.integers(min_value=1, max_value=10**9),
)
def test_an_accepted_refund_never_exceeds_the_capture(
    captured: int, refunds: list[tuple[RefundState, int]], amount: int
) -> None:
    try:
        check_refund(amount, captured, refunds)
    except RefundExceedsRefundable:
        assert amount > refundable_amount(captured, refunds)
        return
    reserved = sum(value for state, value in refunds if state not in {R.DENIED, R.CANCELLED})
    assert reserved + amount <= captured
