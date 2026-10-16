"""Refunds (docs/DOMAIN.md s2, FINANCIAL_MODEL R8): full or partial, never above the refundable.

States: ``requested -> submitted -> done`` · ``submitted -> denied | cancelled`` ·
``submitted -> unknown -> (lookup) done | denied``. ``requested`` is what the hub committed before
calling the provider (ADR-0012 s3), so it may also end ``denied`` (the provider refused it) or
``unknown`` (the call's outcome is unknown); an ``unknown`` refund whose absence the lookup confirms
goes back to ``requested`` for a new attempt. Provider-initiated refunds enter directly as ``done``.
"""

from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Final

from financial_hub.modules.payments.domain.state import IllegalTransition, StaleTransition


class RefundState(StrEnum):
    REQUESTED = "requested"
    SUBMITTED = "submitted"
    UNKNOWN = "unknown"
    DONE = "done"
    DENIED = "denied"
    CANCELLED = "cancelled"


TERMINAL: Final = frozenset({RefundState.DONE, RefundState.DENIED, RefundState.CANCELLED})
# Refunds that hold (or may hold) money: they count against the refundable amount.
RESERVING: Final = frozenset(
    {RefundState.REQUESTED, RefundState.SUBMITTED, RefundState.UNKNOWN, RefundState.DONE}
)

REFUND_MOVES: Final[Mapping[RefundState, frozenset[RefundState]]] = {
    RefundState.REQUESTED: frozenset(
        {RefundState.SUBMITTED, RefundState.DONE, RefundState.DENIED, RefundState.UNKNOWN}
    ),
    RefundState.SUBMITTED: frozenset(
        {RefundState.DONE, RefundState.DENIED, RefundState.CANCELLED, RefundState.UNKNOWN}
    ),
    RefundState.UNKNOWN: frozenset(
        {RefundState.SUBMITTED, RefundState.DONE, RefundState.DENIED, RefundState.REQUESTED}
    ),
    RefundState.DONE: frozenset(),
    RefundState.DENIED: frozenset(),
    RefundState.CANCELLED: frozenset(),
}
REFUND_RANK: Final[Mapping[RefundState, int]] = {
    RefundState.REQUESTED: 0,
    RefundState.UNKNOWN: 1,
    RefundState.SUBMITTED: 2,
    RefundState.DONE: 3,
    RefundState.DENIED: 3,
    RefundState.CANCELLED: 3,
}


def move_refund(current: RefundState, target: RefundState) -> RefundState:
    """The refund's next state; same state is a no-op, an earlier one is stale."""
    if target == current or target in REFUND_MOVES[current]:
        return target
    if REFUND_RANK[target] < REFUND_RANK[current]:
        raise StaleTransition("refund_status", current.value, target.value)
    raise IllegalTransition("refund_status", current.value, target.value)


class RefundExceedsRefundable(ValueError):
    def __init__(self, amount: int, refundable: int) -> None:
        super().__init__(f"refund of {amount} exceeds the refundable {refundable}")
        self.amount = amount
        self.refundable = refundable


def refunded_amount(refunds: Iterable[tuple[RefundState, int]]) -> int:
    """Sum of the refunds in state ``done`` (what ``refund_status`` derives from)."""
    return sum(amount for state, amount in refunds if state is RefundState.DONE)


def refundable_amount(captured_amount: int, refunds: Iterable[tuple[RefundState, int]]) -> int:
    """What may still be refunded: captured minus every refund that holds money."""
    reserved = sum(amount for state, amount in refunds if state in RESERVING)
    return max(captured_amount - reserved, 0)


def check_refund(
    amount: int, captured_amount: int, refunds: Iterable[tuple[RefundState, int]]
) -> int:
    """Validate a new refund of ``amount`` minor units; returns the refundable before it."""
    if type(amount) is not int or amount <= 0:
        raise ValueError("a refund amount must be a positive int in minor units")
    refundable = refundable_amount(captured_amount, refunds)
    if amount > refundable:
        raise RefundExceedsRefundable(amount, refundable)
    return refundable
