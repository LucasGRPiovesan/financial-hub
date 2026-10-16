"""The Asaas status table (ADR-0012 s1): every documented status is mapped, none is guessed."""

import pytest

from financial_hub.modules.payments.domain import (
    CaptureStatus,
    DisputeStatus,
    RefundState,
    SettlementStatus,
)
from financial_hub.modules.payments.infrastructure import asaas_status
from financial_hub.modules.payments.infrastructure.asaas_status import (
    REFUND_TABLE,
    STATUS_TABLE,
    refund_state,
    status_effect,
)

# Written out independently of the table: docs/research/asaas-sandbox.md s3 (the REST enum) plus
# AUTHORIZED, which the webhooks name but the enum omits. A status added here without a mapping, or
# mapped without being documented, fails the tests below.
DOCUMENTED_PAYMENT_STATUSES = frozenset(
    {
        "PENDING",
        "RECEIVED",
        "CONFIRMED",
        "OVERDUE",
        "REFUNDED",
        "RECEIVED_IN_CASH",
        "REFUND_REQUESTED",
        "REFUND_IN_PROGRESS",
        "CHARGEBACK_REQUESTED",
        "CHARGEBACK_DISPUTE",
        "AWAITING_CHARGEBACK_REVERSAL",
        "DUNNING_REQUESTED",
        "DUNNING_RECEIVED",
        "AWAITING_RISK_ANALYSIS",
        "AUTHORIZED",
    }
)
DOCUMENTED_REFUND_STATUSES = frozenset({"PENDING", "CANCELLED", "DONE"})

EXPECTED = {
    "PENDING": (CaptureStatus.PENDING, SettlementStatus.NONE, DisputeStatus.NONE),
    "OVERDUE": (CaptureStatus.PENDING, SettlementStatus.NONE, DisputeStatus.NONE),
    "AWAITING_RISK_ANALYSIS": (CaptureStatus.PENDING, SettlementStatus.NONE, DisputeStatus.NONE),
    "AUTHORIZED": (CaptureStatus.PENDING, SettlementStatus.NONE, DisputeStatus.NONE),
    "DUNNING_REQUESTED": (CaptureStatus.PENDING, SettlementStatus.NONE, DisputeStatus.NONE),
    "CONFIRMED": (CaptureStatus.CAPTURED, SettlementStatus.NONE, DisputeStatus.NONE),
    "RECEIVED": (CaptureStatus.CAPTURED, SettlementStatus.SETTLED, DisputeStatus.NONE),
    "RECEIVED_IN_CASH": (CaptureStatus.CAPTURED, SettlementStatus.NONE, DisputeStatus.NONE),
    "DUNNING_RECEIVED": (CaptureStatus.CAPTURED, SettlementStatus.SETTLED, DisputeStatus.NONE),
    "REFUNDED": (CaptureStatus.CAPTURED, SettlementStatus.NONE, DisputeStatus.NONE),
    "REFUND_REQUESTED": (CaptureStatus.CAPTURED, SettlementStatus.NONE, DisputeStatus.NONE),
    "REFUND_IN_PROGRESS": (CaptureStatus.CAPTURED, SettlementStatus.NONE, DisputeStatus.NONE),
    "CHARGEBACK_REQUESTED": (CaptureStatus.CAPTURED, SettlementStatus.NONE, DisputeStatus.OPEN),
    "CHARGEBACK_DISPUTE": (CaptureStatus.CAPTURED, SettlementStatus.NONE, DisputeStatus.OPEN),
    "AWAITING_CHARGEBACK_REVERSAL": (
        CaptureStatus.CAPTURED,
        SettlementStatus.NONE,
        DisputeStatus.OPEN,
    ),
}


def test_every_documented_status_is_mapped_and_nothing_else() -> None:
    assert set(STATUS_TABLE) == DOCUMENTED_PAYMENT_STATUSES
    assert set(EXPECTED) == DOCUMENTED_PAYMENT_STATUSES


@pytest.mark.parametrize("raw_status", sorted(DOCUMENTED_PAYMENT_STATUSES))
def test_a_documented_status_maps_to_its_orthogonal_fields(raw_status: str) -> None:
    effect = status_effect(raw_status)

    assert effect is not None, f"{raw_status} is unmapped"
    assert (effect.capture, effect.settlement, effect.dispute) == EXPECTED[raw_status]


@pytest.mark.parametrize("raw_status", ["", "pending", "SOMETHING_NEW", "DELETED"])
def test_an_undocumented_status_is_not_in_the_table(raw_status: str) -> None:
    assert status_effect(raw_status) is None


def test_unknown_and_deleted_effects_are_neutral_and_terminal() -> None:
    assert asaas_status.UNKNOWN.capture is CaptureStatus.PENDING
    assert asaas_status.UNKNOWN.settlement is SettlementStatus.NONE
    assert asaas_status.UNKNOWN.dispute is DisputeStatus.NONE
    assert asaas_status.DELETED.capture is CaptureStatus.CANCELED
    assert asaas_status.DELETED_RAW_STATUS == "DELETED"


def test_no_status_marks_a_payment_failed_or_unpaid_after_capture() -> None:
    # a refund/chargeback status must never move capture backwards (forward-only snapshots)
    for effect in STATUS_TABLE.values():
        assert effect.capture in {CaptureStatus.PENDING, CaptureStatus.CAPTURED}


def test_every_documented_refund_status_is_mapped() -> None:
    assert set(REFUND_TABLE) == DOCUMENTED_REFUND_STATUSES
    assert refund_state("PENDING") is RefundState.SUBMITTED
    assert refund_state("DONE") is RefundState.DONE
    assert refund_state("CANCELLED") is RefundState.CANCELLED
    assert refund_state("WHATEVER") is None
