"""Asaas payment/refund statuses -> the orthogonal state fields of ADR-0012 s1 (pure tables).

A payment status is not a target status: it is a *snapshot* of the three provider-facing fields
(``capture_status``, ``settlement_status``, ``dispute_status``). The service moves each field
forward only (``apply_snapshot``), so a status that says "not settled" never undoes a settlement
the hub already recorded; refunds are read from ``refunds[]``, not from the status.

Every status of the REST enum (docs/research/asaas-sandbox.md s3) plus the one the webhooks name
but the enum omits (``AUTHORIZED``, a DOC-CONFLICT) is mapped explicitly in :data:`STATUS_TABLE`.
A status that is in neither is *unknown*: it maps to the neutral "nothing happened yet" effect, the
raw status is kept on the snapshot and the caller logs it (research s8.10: never crash on it).
"""

from dataclasses import dataclass
from typing import Final

from financial_hub.modules.payments.domain import (
    CaptureStatus,
    DisputeStatus,
    RefundState,
    SettlementStatus,
)


@dataclass(frozen=True, slots=True)
class StatusEffect:
    capture: CaptureStatus
    settlement: SettlementStatus = SettlementStatus.NONE
    dispute: DisputeStatus = DisputeStatus.NONE


_PENDING = StatusEffect(CaptureStatus.PENDING)
_CAPTURED = StatusEffect(CaptureStatus.CAPTURED)
_SETTLED = StatusEffect(CaptureStatus.CAPTURED, SettlementStatus.SETTLED)
_DISPUTED = StatusEffect(CaptureStatus.CAPTURED, dispute=DisputeStatus.OPEN)

# Mapping rationale, per status:
# - PENDING / OVERDUE: unpaid. ``OVERDUE`` is a due-date fact the hub derives itself (public status
#   ``overdue`` = pending past ``due_date``), so capture stays ``pending``.
# - AWAITING_RISK_ANALYSIS / AUTHORIZED: a card is authorized but not captured yet.
# - CONFIRMED: paid, balance not yet available (card) -> captured, not settled.
# - RECEIVED: paid and the funds are available -> captured + settled (PIX/boleto jump here).
# - RECEIVED_IN_CASH: the merchant marked it paid outside Asaas: the payer paid (captured) but no
#   funds ever reach the Asaas balance, so it is never settled.
# - REFUNDED / REFUND_*: it was paid (captured); the refund amounts come from ``refunds[]``.
#   Settlement is left ``none``: forward-only moves keep an earlier ``settled``.
# - CHARGEBACK_REQUESTED / CHARGEBACK_DISPUTE: a dispute is open on a captured payment.
# - AWAITING_CHARGEBACK_REVERSAL: ``open`` again (ADR-0012: a decided dispute may reopen).
# - DUNNING_REQUESTED: still unpaid. DUNNING_RECEIVED: the debt was recovered, i.e. paid and
#   credited -> captured + settled.
STATUS_TABLE: Final[dict[str, StatusEffect]] = {
    "PENDING": _PENDING,
    "OVERDUE": _PENDING,
    "AWAITING_RISK_ANALYSIS": _PENDING,
    "AUTHORIZED": _PENDING,
    "DUNNING_REQUESTED": _PENDING,
    "CONFIRMED": _CAPTURED,
    "RECEIVED": _SETTLED,
    "RECEIVED_IN_CASH": _CAPTURED,
    "DUNNING_RECEIVED": _SETTLED,
    "REFUNDED": _CAPTURED,
    "REFUND_REQUESTED": _CAPTURED,
    "REFUND_IN_PROGRESS": _CAPTURED,
    "CHARGEBACK_REQUESTED": _DISPUTED,
    "CHARGEBACK_DISPUTE": _DISPUTED,
    "AWAITING_CHARGEBACK_REVERSAL": _DISPUTED,
}
# A deleted charge keeps its last status in the API and carries ``deleted: true``.
DELETED: Final = StatusEffect(CaptureStatus.CANCELED)
DELETED_RAW_STATUS: Final = "DELETED"
UNKNOWN: Final = _PENDING


def status_effect(raw_status: str) -> StatusEffect | None:
    """The effect of a documented status; ``None`` when the status is not in the table."""
    return STATUS_TABLE.get(raw_status)


# Asaas ``refunds[].status``: PENDING | CANCELLED | DONE (research s3).
REFUND_TABLE: Final[dict[str, RefundState]] = {
    "PENDING": RefundState.SUBMITTED,
    "DONE": RefundState.DONE,
    "CANCELLED": RefundState.CANCELLED,
}


def refund_state(raw_status: str) -> RefundState | None:
    return REFUND_TABLE.get(raw_status)
