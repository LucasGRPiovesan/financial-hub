"""Reconciliation vocabulary (ADR-0013, docs/RECONCILIATION.md): runs, breaks, adjustments.

Pure: no I/O. Amounts are integer minor units; nothing here ever touches a float.
"""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Final
from uuid import UUID

from financial_hub.shared.errors import Conflict, FieldError, ValidationFailed

MAX_WINDOW_DAYS: Final = 31
MAX_REASON_LENGTH: Final = 1000


class RunKind(StrEnum):
    INTERNAL = "internal"
    TRANSACTIONS = "transactions"
    BALANCE = "balance"


class RunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class BreakKind(StrEnum):
    LEDGER_DRIFT = "ledger_drift"
    CLEARING_NOT_ZERO = "clearing_not_zero"
    MISSING_INTERNAL = "missing_internal"
    MISSING_EXTERNAL = "missing_external"
    AMOUNT_MISMATCH = "amount_mismatch"
    FEE_MISMATCH = "fee_mismatch"
    STATUS_MISMATCH = "status_mismatch"
    BALANCE_DRIFT = "balance_drift"


# A refresh from the provider re-applies the payment through the normal transitions: a missed
# webhook (status) or a fee the provider revised (R2a). Everything else needs an operator.
AUTO_RESOLVABLE: Final = frozenset({BreakKind.STATUS_MISMATCH, BreakKind.FEE_MISMATCH})


class BreakState(StrEnum):
    OPEN = "open"
    INVESTIGATING = "investigating"
    RESOLVED = "resolved"
    ACCEPTED = "accepted"


ACTIVE_STATES: Final = frozenset({BreakState.OPEN, BreakState.INVESTIGATING})
REASON_REQUIRED: Final = frozenset({BreakState.INVESTIGATING, BreakState.ACCEPTED})
_TRANSITIONS: Final[Mapping[BreakState, frozenset[BreakState]]] = {
    BreakState.OPEN: frozenset(
        {BreakState.INVESTIGATING, BreakState.RESOLVED, BreakState.ACCEPTED}
    ),
    BreakState.INVESTIGATING: frozenset({BreakState.RESOLVED, BreakState.ACCEPTED}),
    BreakState.RESOLVED: frozenset(),
    BreakState.ACCEPTED: frozenset(),
}


class AdjustmentStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"


class IllegalBreakTransition(Conflict):
    slug = "illegal-break-transition"
    title = "The break cannot move to this state."


def _invalid(pointer: str, detail: str, code: str = "invalid") -> ValidationFailed:
    return ValidationFailed(
        "The request contains invalid fields.",
        errors=[FieldError(pointer=pointer, detail=detail, code=code)],
    )


def check_reason(reason: str | None, *, required: bool, pointer: str = "/reason") -> str | None:
    """A trimmed reason; required ones must be non-blank. Never more than 1000 characters."""
    cleaned = None if reason is None else reason.strip() or None
    if cleaned is None:
        if required:
            raise _invalid(pointer, "a reason is required", "required")
        return None
    if len(cleaned) > MAX_REASON_LENGTH:
        raise _invalid(pointer, f"at most {MAX_REASON_LENGTH} characters")
    return cleaned


def check_transition(current: BreakState, target: BreakState, reason: str | None) -> str | None:
    """The cleaned reason, if ``current -> target`` is legal (else ``IllegalBreakTransition``)."""
    if target not in _TRANSITIONS[current]:
        raise IllegalBreakTransition(f"A break cannot move from {current} to {target}.")
    return check_reason(reason, required=target in REASON_REQUIRED)


@dataclass(frozen=True, slots=True)
class Window:
    """An inclusive range of calendar days (America/Sao_Paulo, the provider's calendar)."""

    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise _invalid("/window_end", "window_end must not be before window_start")
        if (self.end - self.start).days + 1 > MAX_WINDOW_DAYS:
            raise _invalid("/window_end", f"a window spans at most {MAX_WINDOW_DAYS} days")

    @classmethod
    def day_before(cls, today: date) -> Window:
        yesterday = today - timedelta(days=1)
        return cls(yesterday, yesterday)


@dataclass(frozen=True, slots=True)
class BreakDraft:
    """A detected discrepancy. ``fingerprint`` identifies "the same break" across runs."""

    kind: BreakKind
    fingerprint: str
    subject: Mapping[str, str]
    internal: Mapping[str, object] | None
    external: Mapping[str, object] | None
    currency: str | None = None
    difference: int | None = None
    payment_intent_id: UUID | None = None
    provider_payment_id: str | None = None
    account_id: UUID | None = None
    extra: Mapping[str, object] = field(default_factory=dict[str, object])


def fingerprint(kind: BreakKind, *parts: object) -> str:
    return ":".join([kind.value, *(str(part) for part in parts)])


def adjustment_payload_hash(
    *,
    break_id: UUID,
    provider_account_id: UUID,
    currency: str,
    amount: int,
    reason: str,
) -> str:
    """SHA-256 of what is approved: the approver signs off on exactly this payload."""
    canonical = json.dumps(
        {
            "amount": amount,
            "break_id": str(break_id),
            "currency": currency,
            "provider_account_id": str(provider_account_id),
            "reason": reason,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Run:
    """A stored run; ``provider`` is ``''`` for internal runs."""

    id: UUID
    project_id: UUID
    kind: RunKind
    provider: str
    provider_account_id: UUID | None
    window: Window
    status: RunStatus
    triggered_by: str
    summary: Mapping[str, object]
    breaks_opened: int
    error_code: str | None
    started_at: datetime
    completed_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Break:
    """A stored break; ``history`` is the append-only log of its state changes."""

    id: UUID
    project_id: UUID
    run_id: UUID
    last_run_id: UUID
    kind: BreakKind
    state: BreakState
    fingerprint: str
    subject: Mapping[str, object]
    internal: Mapping[str, object] | None
    external: Mapping[str, object] | None
    currency: str | None
    difference: int | None
    payment_intent_id: UUID | None
    provider_payment_id: str | None
    account_id: UUID | None
    history: tuple[Mapping[str, object], ...]
    resolution: str | None
    resolved_by: str | None
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Adjustment:
    """A proposed R12 entry; ``amount`` is signed (``> 0`` debits ``provider_balance``)."""

    id: UUID
    project_id: UUID
    break_id: UUID
    provider_account_id: UUID
    currency: str
    amount: int
    reason: str
    payload_hash: str
    status: AdjustmentStatus
    proposed_by: UUID
    decided_by: UUID | None
    decision_reason: str | None
    journal_entry_id: UUID | None
    created_at: datetime
    decided_at: datetime | None
