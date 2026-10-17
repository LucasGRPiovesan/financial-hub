"""Reconciliation event types (ADR-0005). Payloads carry ids, kinds and states: never amounts."""

from financial_hub.modules.events.registry import EventPayload, ScheduleDuePayload, registry


class RunCompletedPayload(EventPayload):
    run_id: str
    kind: str
    status: str
    window_start: str
    window_end: str
    breaks_opened: int


class BreakOpenedPayload(EventPayload):
    break_id: str
    run_id: str
    kind: str


class BreakResolvedPayload(EventPayload):
    break_id: str
    kind: str
    state: str
    automatic: bool


class ReconciliationRunDuePayload(ScheduleDuePayload):
    pass


RUN_COMPLETED = registry.register(
    "reconciliation.run_completed",
    RunCompletedPayload,
    description="A reconciliation run finished (`completed` or `failed`); fetch it for details.",
)
BREAK_OPENED = registry.register(
    "reconciliation.break_opened",
    BreakOpenedPayload,
    description="A run found a new discrepancy (a re-detected open break emits nothing).",
)
BREAK_RESOLVED = registry.register(
    "reconciliation.break_resolved",
    BreakResolvedPayload,
    description=(
        "A break was closed: `resolved` (fixed, by refresh or an approved adjustment) or "
        "`accepted` (explained, nothing to change). `automatic` marks auto-resolution."
    ),
)

# --- internal: consumed by the reconciliation handler, never sent to customer endpoints ---------

RUN_DUE = registry.register(
    "reconciliation.run_due",
    ReconciliationRunDuePayload,
    description="Internal: the daily reconciliation schedule is due.",
)
