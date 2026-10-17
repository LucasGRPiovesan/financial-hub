"""Reconciliation: runs, breaks and four-eyes adjustments (docs/RECONCILIATION.md).

All under ``/v1/projects/{project_id}/reconciliation``:

- ``runs``: ``reconciliation:read`` lists and reads; ``reconciliation:manage`` starts one
  (idempotent by kind + account + window: the same request returns the stored run).
- ``breaks``: ``reconciliation:read``; ``POST breaks/{id}/transition`` needs
  ``reconciliation:manage`` (``investigating`` and ``accepted`` need a reason).
- ``breaks/{id}/adjustments`` and ``adjustments/{id}/approve|reject``: ``ledger:adjust`` and an
  operator session; the approver must differ from the proposer and must send the payload hash
  they reviewed. Money is posted (R12) only on approval.
"""

from datetime import date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, Field

from financial_hub.modules.audit.application import AuditContext, request_audit_context
from financial_hub.modules.identity.application.api import Permission
from financial_hub.modules.payments.application.api import ProviderRegistry, build_registry
from financial_hub.modules.reconciliation.application.api import (
    Adjustment,
    Break,
    ReconciliationService,
    Run,
    build_reconciliation_service,
)
from financial_hub.platform.http.auth import ProjectScope, require
from financial_hub.platform.http.problems import Problem
from financial_hub.platform.http.protection.idempotency import idempotent
from financial_hub.shared.ids import IdPrefix, format_id
from financial_hub.shared.pagination import Page, PageParams

router = APIRouter(prefix="/v1/projects/{project_id}/reconciliation", tags=["reconciliation"])

ReadReconciliation = Annotated[ProjectScope, require(Permission.RECONCILIATION_READ)]
ManageReconciliation = Annotated[ProjectScope, require(Permission.RECONCILIATION_MANAGE)]
AdjustLedger = Annotated[ProjectScope, require(Permission.LEDGER_ADJUST)]
AuditCtx = Annotated[AuditContext, Depends(request_audit_context)]

_CONFLICT: dict[int | str, dict[str, Any]] = {
    status.HTTP_409_CONFLICT: {"model": Problem, "description": "State changed."}
}
_FORBIDDEN: dict[int | str, dict[str, Any]] = {
    status.HTTP_403_FORBIDDEN: {"model": Problem, "description": "Not an operator, or four-eyes."}
}


def _id(prefix: IdPrefix, value: Any) -> str | None:
    return None if value is None else format_id(prefix, value)


class RunOut(BaseModel):
    id: str
    kind: str = Field(description="`internal`, `transactions` or `balance`.")
    provider: str | None
    provider_account_id: str | None
    window_start: date = Field(description="First day (America/Sao_Paulo), inclusive.")
    window_end: date = Field(description="Last day, inclusive.")
    status: str = Field(description="`running`, `completed` or `failed`.")
    triggered_by: str
    summary: dict[str, Any] = Field(description="Counts; amounts in integer minor units.")
    breaks_opened: int
    error_code: str | None = Field(description="Provider error code of a failed run.")
    started_at: datetime
    completed_at: datetime | None
    created_at: datetime

    @classmethod
    def of(cls, run: Run) -> RunOut:
        return cls(
            id=format_id(IdPrefix.RECONCILIATION_RUN, run.id),
            kind=run.kind.value,
            provider=run.provider or None,
            provider_account_id=_id(IdPrefix.PROVIDER_ACCOUNT, run.provider_account_id),
            window_start=run.window.start,
            window_end=run.window.end,
            status=run.status.value,
            triggered_by=run.triggered_by,
            summary=dict(run.summary),
            breaks_opened=run.breaks_opened,
            error_code=run.error_code,
            started_at=run.started_at,
            completed_at=run.completed_at,
            created_at=run.created_at,
        )


class BreakOut(BaseModel):
    id: str
    run_id: str = Field(description="The run that opened it.")
    last_run_id: str = Field(description="The last run that detected it.")
    kind: str
    state: str = Field(description="`open`, `investigating`, `resolved` or `accepted`.")
    subject: dict[str, Any]
    internal: dict[str, Any] | None = Field(description="The hub side (integer minor units).")
    external: dict[str, Any] | None = Field(description="The provider / recomputed side.")
    currency: str | None
    difference_minor: int | None = Field(description="External minus internal.")
    payment_id: str | None
    provider_payment_id: str | None
    account_id: str | None
    history: list[dict[str, Any]]
    resolution: str | None
    resolved_by: str | None
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, item: Break) -> BreakOut:
        return cls(
            id=format_id(IdPrefix.RECONCILIATION_BREAK, item.id),
            run_id=format_id(IdPrefix.RECONCILIATION_RUN, item.run_id),
            last_run_id=format_id(IdPrefix.RECONCILIATION_RUN, item.last_run_id),
            kind=item.kind.value,
            state=item.state.value,
            subject=dict(item.subject),
            internal=None if item.internal is None else dict(item.internal),
            external=None if item.external is None else dict(item.external),
            currency=item.currency,
            difference_minor=item.difference,
            payment_id=_id(IdPrefix.PAYMENT_INTENT, item.payment_intent_id),
            provider_payment_id=item.provider_payment_id,
            account_id=_id(IdPrefix.ACCOUNT, item.account_id),
            history=[dict(entry) for entry in item.history],
            resolution=item.resolution,
            resolved_by=item.resolved_by,
            resolved_at=item.resolved_at,
            created_at=item.created_at,
            updated_at=item.updated_at,
        )


class AdjustmentOut(BaseModel):
    id: str
    break_id: str
    provider_account_id: str
    currency: str
    amount_minor: int = Field(description="> 0 debits provider_balance, < 0 credits it.")
    reason: str
    payload_hash: str = Field(description="SHA-256 the approver must send back.")
    status: str = Field(description="`proposed`, `approved` or `rejected`.")
    proposed_by: str
    decided_by: str | None
    decision_reason: str | None
    journal_entry_id: str | None = Field(description="The R12 entry, once approved.")
    created_at: datetime
    decided_at: datetime | None

    @classmethod
    def of(cls, item: Adjustment) -> AdjustmentOut:
        return cls(
            id=format_id(IdPrefix.RECONCILIATION_ADJUSTMENT, item.id),
            break_id=format_id(IdPrefix.RECONCILIATION_BREAK, item.break_id),
            provider_account_id=format_id(IdPrefix.PROVIDER_ACCOUNT, item.provider_account_id),
            currency=item.currency,
            amount_minor=item.amount,
            reason=item.reason,
            payload_hash=item.payload_hash,
            status=item.status.value,
            proposed_by=format_id(IdPrefix.OPERATOR, item.proposed_by),
            decided_by=_id(IdPrefix.OPERATOR, item.decided_by),
            decision_reason=item.decision_reason,
            journal_entry_id=_id(IdPrefix.JOURNAL_ENTRY, item.journal_entry_id),
            created_at=item.created_at,
            decided_at=item.decided_at,
        )


class StartRunRequest(BaseModel):
    kind: str = Field(max_length=16, description="`internal`, `transactions` or `balance`.")
    window_start: date | None = Field(default=None, description="Default: yesterday.")
    window_end: date | None = Field(default=None, description="At most 31 days, before today.")
    provider_account_id: str | None = Field(
        default=None, max_length=64, description="`pva_...`; required if the project has several."
    )


class TransitionRequest(BaseModel):
    state: str = Field(max_length=16, description="`investigating`, `resolved` or `accepted`.")
    reason: str | None = Field(default=None, max_length=1000)


class ProposeAdjustmentRequest(BaseModel):
    amount_minor: int = Field(description="Signed integer minor units, non-zero.")
    currency: str | None = Field(default=None, max_length=3, description="Default: the break's.")
    reason: str = Field(max_length=1000)
    provider_account_id: str | None = Field(
        default=None, max_length=64, description="`pva_...`; default: the break's account."
    )


class ApproveAdjustmentRequest(BaseModel):
    payload_hash: str = Field(min_length=64, max_length=64)


class RejectAdjustmentRequest(BaseModel):
    reason: str = Field(max_length=1000)


def _registry(request: Request) -> ProviderRegistry:
    state = request.app.state
    registry: ProviderRegistry | None = getattr(state, "payment_providers", None)
    if registry is None:
        registry = build_registry(state.settings)
        state.payment_providers = registry
    return registry


def _service(request: Request, audit: AuditContext | None = None) -> ReconciliationService:
    state = request.app.state
    return build_reconciliation_service(
        state.session_factory,
        clock=state.clock,
        registry=_registry(request),
        credentials=getattr(state, "payment_credentials", None),
        contacts=getattr(state, "payment_contacts", None),
        audit=audit,
    )


# --- runs ---------------------------------------------------------------------------------------


@router.post("/runs", responses=_CONFLICT)
async def start_run(
    body: StartRunRequest,
    scope: ManageReconciliation,
    request: Request,
    audit: AuditCtx,
    _idempotency: Annotated[None, idempotent()],
) -> RunOut:
    """Reconcile one window now, or return the stored run of that window (idempotent)."""
    run = await _service(request, audit).start_run(
        scope,
        kind=body.kind,
        window_start=body.window_start,
        window_end=body.window_end,
        provider_account_id=body.provider_account_id,
    )
    return RunOut.of(run)


class ListRunsQuery(PageParams):
    kind: str | None = Field(default=None, max_length=16)
    status: str | None = Field(default=None, max_length=16)


@router.get("/runs")
async def list_runs(
    scope: ReadReconciliation, params: Annotated[ListRunsQuery, Query()], request: Request
) -> Page[RunOut]:
    page = await _service(request).list_runs(
        scope.project_id, params, kind=params.kind, status=params.status
    )
    return Page[RunOut](
        data=[RunOut.of(run) for run in page.data],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


@router.get("/runs/{run_id}")
async def get_run(run_id: str, scope: ReadReconciliation, request: Request) -> RunOut:
    return RunOut.of(await _service(request).get_run(scope.project_id, run_id))


# --- breaks -------------------------------------------------------------------------------------


class ListBreaksQuery(PageParams):
    kind: str | None = Field(default=None, max_length=32)
    state: str | None = Field(default=None, max_length=16)
    run_id: str | None = Field(default=None, max_length=64)


@router.get("/breaks")
async def list_breaks(
    scope: ReadReconciliation, params: Annotated[ListBreaksQuery, Query()], request: Request
) -> Page[BreakOut]:
    page = await _service(request).list_breaks(
        scope.project_id, params, kind=params.kind, state=params.state, run_id=params.run_id
    )
    return Page[BreakOut](
        data=[BreakOut.of(item) for item in page.data],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


@router.get("/breaks/{break_id}")
async def get_break(break_id: str, scope: ReadReconciliation, request: Request) -> BreakOut:
    return BreakOut.of(await _service(request).get_break(scope.project_id, break_id))


@router.post("/breaks/{break_id}/transition", responses=_CONFLICT)
async def transition_break(
    break_id: str,
    body: TransitionRequest,
    scope: ManageReconciliation,
    request: Request,
    audit: AuditCtx,
) -> BreakOut:
    """``open -> investigating -> resolved | accepted``; a closed break never reopens."""
    moved = await _service(request, audit).transition_break(
        scope, break_id, state=body.state, reason=body.reason
    )
    return BreakOut.of(moved)


# --- adjustments --------------------------------------------------------------------------------


@router.post(
    "/breaks/{break_id}/adjustments",
    status_code=status.HTTP_201_CREATED,
    responses=_CONFLICT | _FORBIDDEN,
)
async def propose_adjustment(
    break_id: str,
    body: ProposeAdjustmentRequest,
    scope: AdjustLedger,
    request: Request,
    audit: AuditCtx,
    _idempotency: Annotated[None, idempotent()],
) -> AdjustmentOut:
    """Propose an R12 entry for the break. Nothing is posted until another operator approves."""
    adjustment = await _service(request, audit).propose_adjustment(
        scope,
        break_id,
        amount=body.amount_minor,
        currency=body.currency,
        reason=body.reason,
        provider_account_id=body.provider_account_id,
    )
    return AdjustmentOut.of(adjustment)


class ListAdjustmentsQuery(PageParams):
    break_id: str | None = Field(default=None, max_length=64)
    status: str | None = Field(default=None, max_length=16)


@router.get("/adjustments")
async def list_adjustments(
    scope: ReadReconciliation, params: Annotated[ListAdjustmentsQuery, Query()], request: Request
) -> Page[AdjustmentOut]:
    page = await _service(request).list_adjustments(
        scope.project_id, params, break_id=params.break_id, status=params.status
    )
    return Page[AdjustmentOut](
        data=[AdjustmentOut.of(item) for item in page.data],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


@router.get("/adjustments/{adjustment_id}")
async def get_adjustment(
    adjustment_id: str, scope: ReadReconciliation, request: Request
) -> AdjustmentOut:
    return AdjustmentOut.of(await _service(request).get_adjustment(scope.project_id, adjustment_id))


@router.post("/adjustments/{adjustment_id}/approve", responses=_CONFLICT | _FORBIDDEN)
async def approve_adjustment(
    adjustment_id: str,
    body: ApproveAdjustmentRequest,
    scope: AdjustLedger,
    request: Request,
    audit: AuditCtx,
) -> AdjustmentOut:
    """Post the R12 entry and resolve the break (a different operator than the proposer)."""
    adjustment = await _service(request, audit).approve_adjustment(
        scope, adjustment_id, payload_hash=body.payload_hash
    )
    return AdjustmentOut.of(adjustment)


@router.post("/adjustments/{adjustment_id}/reject", responses=_CONFLICT | _FORBIDDEN)
async def reject_adjustment(
    adjustment_id: str,
    body: RejectAdjustmentRequest,
    scope: AdjustLedger,
    request: Request,
    audit: AuditCtx,
) -> AdjustmentOut:
    """Reject the proposal: nothing is posted, the break stays as it was."""
    adjustment = await _service(request, audit).reject_adjustment(
        scope, adjustment_id, reason=body.reason
    )
    return AdjustmentOut.of(adjustment)
