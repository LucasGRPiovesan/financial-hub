"""Orthogonal payment state (ADR-0012 s1) and the public status precedence (docs/DOMAIN.md s2).

A ``ProviderPayment`` carries four independent fields instead of one enum. Each field only moves
forward; the transition functions here are the single place that knows the allowed moves:

- a move to the current value returns the state unchanged (idempotent replays);
- a move to an *earlier* value raises :class:`StaleTransition` (event handlers ignore it and record
  ``stale`` on the receipt);
- any other disallowed move raises :class:`IllegalTransition` (a contradiction: refetch).

``PaymentIntent.status`` is never stored: :func:`public_status` derives it.
"""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date
from enum import StrEnum
from typing import Final


class CaptureStatus(StrEnum):
    PENDING = "pending"
    CAPTURED = "captured"
    FAILED = "failed"
    CANCELED = "canceled"


class SettlementStatus(StrEnum):
    NONE = "none"
    SETTLED = "settled"


class RefundStatus(StrEnum):
    """Derived from the refunds in state ``done`` (never stored)."""

    NONE = "none"
    PARTIAL = "partial"
    FULL = "full"


class DisputeStatus(StrEnum):
    NONE = "none"
    OPEN = "open"
    WON = "won"
    LOST = "lost"


class CreationStatus(StrEnum):
    """Where the intent's provider payment creation stands (ADR-0012 s3 external-call protocol)."""

    CREATE_REQUESTED = "create_requested"  # committed; the provider call may be in flight
    CREATED = "created"  # the provider payment exists and is recorded
    REJECTED = "rejected"  # the provider refused it; nothing exists there
    UNKNOWN = "unknown"  # the outcome is unknown: only a lookup may resolve it


class PaymentStatus(StrEnum):
    """What products see (``PaymentIntent.status``)."""

    REQUIRES_PAYMENT = "requires_payment"
    PROCESSING = "processing"
    OVERDUE = "overdue"
    FAILED = "failed"
    CANCELED = "canceled"
    SUCCEEDED = "succeeded"
    SETTLED = "settled"
    PARTIALLY_REFUNDED = "partially_refunded"
    REFUNDED = "refunded"
    DISPUTED = "disputed"
    CHARGED_BACK = "charged_back"


class IllegalTransition(ValueError):
    """``field`` cannot move from ``current`` to ``target``."""

    def __init__(self, field: str, current: str, target: str, reason: str = "not allowed") -> None:
        super().__init__(f"{field}: {current} -> {target} {reason}")
        self.field = field
        self.current = current
        self.target = target


class StaleTransition(IllegalTransition):
    """The target is earlier than the current value: ignore it and record it as stale."""

    def __init__(self, field: str, current: str, target: str) -> None:
        super().__init__(field, current, target, "is stale (earlier than the current value)")


# Allowed forward moves per field. A capture on an already failed/canceled payment is real (a late
# boleto payment): the move is allowed and the ledger books it as unapplied cash (R2b), never R2.
CAPTURE_MOVES: Final[Mapping[CaptureStatus, frozenset[CaptureStatus]]] = {
    CaptureStatus.PENDING: frozenset(
        {CaptureStatus.CAPTURED, CaptureStatus.FAILED, CaptureStatus.CANCELED}
    ),
    CaptureStatus.FAILED: frozenset({CaptureStatus.CAPTURED}),
    CaptureStatus.CANCELED: frozenset({CaptureStatus.CAPTURED}),
    CaptureStatus.CAPTURED: frozenset(),
}
CAPTURE_RANK: Final[Mapping[CaptureStatus, int]] = {
    CaptureStatus.PENDING: 0,
    CaptureStatus.FAILED: 1,
    CaptureStatus.CANCELED: 1,
    CaptureStatus.CAPTURED: 2,
}
SETTLEMENT_MOVES: Final[Mapping[SettlementStatus, frozenset[SettlementStatus]]] = {
    SettlementStatus.NONE: frozenset({SettlementStatus.SETTLED}),
    SettlementStatus.SETTLED: frozenset(),
}
SETTLEMENT_RANK: Final[Mapping[SettlementStatus, int]] = {
    SettlementStatus.NONE: 0,
    SettlementStatus.SETTLED: 1,
}
# ``open`` may reopen after a decision (Asaas ``AWAITING_CHARGEBACK_REVERSAL``).
DISPUTE_MOVES: Final[Mapping[DisputeStatus, frozenset[DisputeStatus]]] = {
    DisputeStatus.NONE: frozenset({DisputeStatus.OPEN}),
    DisputeStatus.OPEN: frozenset({DisputeStatus.WON, DisputeStatus.LOST}),
    DisputeStatus.WON: frozenset({DisputeStatus.OPEN}),
    DisputeStatus.LOST: frozenset({DisputeStatus.OPEN}),
}
DISPUTE_RANK: Final[Mapping[DisputeStatus, int]] = {
    DisputeStatus.NONE: 0,
    DisputeStatus.OPEN: 1,
    DisputeStatus.WON: 2,
    DisputeStatus.LOST: 2,
}


def _move[S: StrEnum](
    field: str,
    current: S,
    target: S,
    moves: Mapping[S, frozenset[S]],
    rank: Mapping[S, int],
) -> S:
    if target == current or target in moves[current]:
        return target
    if rank[target] < rank[current]:
        raise StaleTransition(field, current.value, target.value)
    raise IllegalTransition(field, current.value, target.value)


@dataclass(frozen=True, slots=True)
class PaymentState:
    """The orthogonal fields of one ``ProviderPayment`` (amounts in integer minor units)."""

    capture_status: CaptureStatus = CaptureStatus.PENDING
    settlement_status: SettlementStatus = SettlementStatus.NONE
    dispute_status: DisputeStatus = DisputeStatus.NONE
    captured_amount: int = 0
    net_amount: int | None = None
    fee_estimated: int | None = None

    def __post_init__(self) -> None:
        for name in ("captured_amount", "net_amount", "fee_estimated"):
            value = getattr(self, name)
            if value is not None and type(value) is not int:
                raise TypeError(f"{name} must be int minor units, got {type(value).__name__}")
        if self.captured_amount < 0:
            raise ValueError("captured_amount must be >= 0")


def move_capture(
    state: PaymentState,
    target: CaptureStatus,
    *,
    amount: int = 0,
    net_amount: int | None = None,
) -> PaymentState:
    """``capture_status`` moves; a capture records its gross ``amount`` (and net, if reported).

    ``captured_amount``/``net_amount``/``fee_estimated`` are set by the capture only; later
    changes of the net are fee adjustments (R2a, PRO-42), not capture transitions.
    """
    status = _move("capture_status", state.capture_status, target, CAPTURE_MOVES, CAPTURE_RANK)
    if status is state.capture_status:
        return state
    if status is not CaptureStatus.CAPTURED:
        return replace(state, capture_status=status)
    if type(amount) is not int or amount <= 0:
        raise ValueError("a capture needs a positive int amount in minor units")
    if net_amount is not None and (type(net_amount) is not int or not 0 <= net_amount <= amount):
        raise ValueError("net_amount must be an int between 0 and the captured amount")
    return replace(
        state,
        capture_status=status,
        captured_amount=amount,
        net_amount=net_amount,
        fee_estimated=None if net_amount is None else amount - net_amount,
    )


def move_settlement(state: PaymentState, target: SettlementStatus) -> PaymentState:
    """``settlement_status`` moves (``none -> settled``); settling requires a captured payment."""
    status = _move(
        "settlement_status", state.settlement_status, target, SETTLEMENT_MOVES, SETTLEMENT_RANK
    )
    if status is not state.settlement_status and state.capture_status is not CaptureStatus.CAPTURED:
        raise IllegalTransition(
            "settlement_status",
            state.settlement_status.value,
            status.value,
            "requires capture_status captured",
        )
    return replace(state, settlement_status=status)


def move_dispute(state: PaymentState, target: DisputeStatus) -> PaymentState:
    """``dispute_status`` moves; a dispute needs a captured payment."""
    status = _move("dispute_status", state.dispute_status, target, DISPUTE_MOVES, DISPUTE_RANK)
    if status is not state.dispute_status and state.capture_status is not CaptureStatus.CAPTURED:
        raise IllegalTransition(
            "dispute_status",
            state.dispute_status.value,
            status.value,
            "requires capture_status captured",
        )
    return replace(state, dispute_status=status)


def refund_status(captured_amount: int, refunded_amount: int) -> RefundStatus:
    """Derived ``refund_status`` from the sum of refunds in state ``done``."""
    if refunded_amount <= 0:
        return RefundStatus.NONE
    if refunded_amount >= captured_amount:
        return RefundStatus.FULL
    return RefundStatus.PARTIAL


def public_status(
    *,
    creation: CreationStatus,
    state: PaymentState | None,
    refund: RefundStatus,
    due_date: date,
    today: date,
) -> PaymentStatus:
    """The derived ``PaymentIntent.status`` (docs/DOMAIN.md s2 precedence, highest first).

    dispute (open -> disputed, lost -> charged_back) > refund (full -> refunded, partial ->
    partially_refunded) > settled > captured -> succeeded > overdue (pending past the due date) >
    failed | canceled > processing > requires_payment.

    ``state`` is ``None`` while no provider payment is recorded (capture is then ``pending``).
    ``processing`` means the creation outcome is not known yet; a rejected creation is ``failed``.
    """
    current = state or PaymentState()
    if current.dispute_status is DisputeStatus.OPEN:
        return PaymentStatus.DISPUTED
    if current.dispute_status is DisputeStatus.LOST:
        return PaymentStatus.CHARGED_BACK
    if refund is RefundStatus.FULL:
        return PaymentStatus.REFUNDED
    if refund is RefundStatus.PARTIAL:
        return PaymentStatus.PARTIALLY_REFUNDED
    if current.settlement_status is SettlementStatus.SETTLED:
        return PaymentStatus.SETTLED
    if current.capture_status is CaptureStatus.CAPTURED:
        return PaymentStatus.SUCCEEDED
    pending = current.capture_status is CaptureStatus.PENDING
    if pending and creation is not CreationStatus.REJECTED and today > due_date:
        return PaymentStatus.OVERDUE
    if current.capture_status is CaptureStatus.FAILED or creation is CreationStatus.REJECTED:
        return PaymentStatus.FAILED
    if current.capture_status is CaptureStatus.CANCELED:
        return PaymentStatus.CANCELED
    if creation in (CreationStatus.CREATE_REQUESTED, CreationStatus.UNKNOWN):
        return PaymentStatus.PROCESSING
    return PaymentStatus.REQUIRES_PAYMENT
