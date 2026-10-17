"""Reconciliation persistence on the caller's session (runs as ``fh_app``, no DELETE).

Every read is scoped by ``project_id`` except the metrics snapshot. State changes are
compare-and-set on the current state, so two operators acting on the same break or adjustment
cannot both win; the database triggers of ``0014_reconciliation`` are the last line of defence.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from financial_hub.modules.reconciliation.domain import (
    ACTIVE_STATES,
    Adjustment,
    AdjustmentStatus,
    Break,
    BreakDraft,
    BreakKind,
    BreakState,
    Run,
    RunKind,
    RunStatus,
    Window,
)
from financial_hub.modules.reconciliation.infrastructure.tables import (
    reconciliation_adjustments,
    reconciliation_breaks,
    reconciliation_runs,
)
from financial_hub.shared.pagination import Cursor, keyset_predicate

_run = reconciliation_runs
_break = reconciliation_breaks
_adjustment = reconciliation_adjustments
_ACTIVE = sorted(state.value for state in ACTIVE_STATES)

# A run still "running" after this long is assumed dead (worker crash) and may be claimed again.
STALE_RUN_AFTER = timedelta(minutes=15)


def _to_run(row: Any) -> Run:
    return Run(
        id=row.id,
        project_id=row.project_id,
        kind=RunKind(row.kind),
        provider=row.provider,
        provider_account_id=row.provider_account_id,
        window=Window(row.window_start, row.window_end),
        status=RunStatus(row.status),
        triggered_by=row.triggered_by,
        summary=dict(row.summary),
        breaks_opened=row.breaks_opened,
        error_code=row.error_code,
        started_at=row.started_at,
        completed_at=row.completed_at,
        created_at=row.created_at,
    )


def _to_break(row: Any) -> Break:
    return Break(
        id=row.id,
        project_id=row.project_id,
        run_id=row.run_id,
        last_run_id=row.last_run_id,
        kind=BreakKind(row.kind),
        state=BreakState(row.state),
        fingerprint=row.fingerprint,
        subject=dict(row.subject),
        internal=None if row.internal is None else dict(row.internal),
        external=None if row.external is None else dict(row.external),
        currency=row.currency,
        difference=row.difference,
        payment_intent_id=row.payment_intent_id,
        provider_payment_id=row.provider_payment_id,
        account_id=row.account_id,
        history=tuple(dict(item) for item in row.history),
        resolution=row.resolution,
        resolved_by=row.resolved_by,
        resolved_at=row.resolved_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_adjustment(row: Any) -> Adjustment:
    return Adjustment(
        id=row.id,
        project_id=row.project_id,
        break_id=row.break_id,
        provider_account_id=row.provider_account_id,
        currency=row.currency,
        amount=row.amount,
        reason=row.reason,
        payload_hash=row.payload_hash,
        status=AdjustmentStatus(row.status),
        proposed_by=row.proposed_by,
        decided_by=row.decided_by,
        decision_reason=row.decision_reason,
        journal_entry_id=row.journal_entry_id,
        created_at=row.created_at,
        decided_at=row.decided_at,
    )


# --- runs ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Claim:
    """``claimed`` is ``True`` when the caller now owns the run and must execute it."""

    run: Run
    claimed: bool


async def claim_run(
    session: AsyncSession,
    *,
    run_id: UUID,
    project_id: UUID,
    kind: RunKind,
    provider: str,
    provider_account_id: UUID | None,
    window: Window,
    triggered_by: str,
    now: datetime,
) -> Claim:
    """The run of this scope: a new one, a failed or stale one reset to ``running``, or the
    existing completed / in-progress one (``claimed=False``: nothing to execute)."""
    account_key = "" if provider_account_id is None else str(provider_account_id)
    statement = (
        insert(_run)
        .values(
            id=run_id,
            project_id=project_id,
            kind=kind.value,
            provider=provider,
            provider_account_id=provider_account_id,
            account_key=account_key,
            window_start=window.start,
            window_end=window.end,
            status=RunStatus.RUNNING.value,
            triggered_by=triggered_by,
            summary={},
            breaks_opened=0,
            started_at=now,
            created_at=now,
        )
        .on_conflict_do_nothing(
            index_elements=[
                _run.c.project_id,
                _run.c.kind,
                _run.c.provider,
                _run.c.account_key,
                _run.c.window_start,
                _run.c.window_end,
            ]
        )
        .returning(*_run.c)
    )
    row = (await session.execute(statement)).first()
    if row is not None:
        return Claim(_to_run(row), claimed=True)
    existing_statement = (
        select(_run)
        .where(
            _run.c.project_id == project_id,
            _run.c.kind == kind.value,
            _run.c.provider == provider,
            _run.c.account_key == account_key,
            _run.c.window_start == window.start,
            _run.c.window_end == window.end,
        )
        .with_for_update()
    )
    existing = _to_run((await session.execute(existing_statement)).one())
    stale = existing.status is RunStatus.RUNNING and existing.started_at < now - STALE_RUN_AFTER
    if existing.status is not RunStatus.FAILED and not stale:
        return Claim(existing, claimed=False)
    reset = (
        update(_run)
        .where(_run.c.id == existing.id)
        .values(
            status=RunStatus.RUNNING.value,
            triggered_by=triggered_by,
            summary={},
            breaks_opened=0,
            error_code=None,
            started_at=now,
            completed_at=None,
        )
        .returning(*_run.c)
    )
    return Claim(_to_run((await session.execute(reset)).one()), claimed=True)


async def finish_run(
    session: AsyncSession,
    run_id: UUID,
    *,
    status: RunStatus,
    summary: Mapping[str, object],
    breaks_opened: int,
    error_code: str | None,
    now: datetime,
) -> Run:
    statement = (
        update(_run)
        .where(_run.c.id == run_id)
        .values(
            status=status.value,
            summary=dict(summary),
            breaks_opened=breaks_opened,
            error_code=error_code,
            completed_at=now,
        )
        .returning(*_run.c)
    )
    return _to_run((await session.execute(statement)).one())


async def get_run(session: AsyncSession, project_id: UUID, run_id: UUID) -> Run | None:
    statement = select(_run).where(_run.c.project_id == project_id, _run.c.id == run_id)
    row = (await session.execute(statement)).first()
    return None if row is None else _to_run(row)


async def list_runs(
    session: AsyncSession,
    project_id: UUID,
    *,
    kind: RunKind | None,
    status: RunStatus | None,
    cursor: Cursor | None,
    limit: int,
) -> Sequence[Run]:
    """Newest first, ``limit`` rows at most (callers pass ``limit + 1`` to detect more)."""
    statement = select(_run).where(_run.c.project_id == project_id)
    if kind is not None:
        statement = statement.where(_run.c.kind == kind.value)
    if status is not None:
        statement = statement.where(_run.c.status == status.value)
    if cursor is not None:
        statement = statement.where(keyset_predicate(_run.c.created_at, _run.c.id, cursor))
    statement = statement.order_by(_run.c.created_at.desc(), _run.c.id.desc()).limit(limit)
    return [_to_run(row) for row in (await session.execute(statement)).all()]


# --- breaks -------------------------------------------------------------------------------------


async def open_break(
    session: AsyncSession,
    *,
    break_id: UUID,
    project_id: UUID,
    run_id: UUID,
    draft: BreakDraft,
    history_entry: Mapping[str, object],
    now: datetime,
) -> tuple[Break, bool]:
    """Open ``draft``, or refresh the active break with its fingerprint (``created=False``).

    A re-detection keeps the state and the history; it moves ``last_run_id`` and the sides.
    """
    statement = (
        insert(_break)
        .values(
            id=break_id,
            project_id=project_id,
            run_id=run_id,
            last_run_id=run_id,
            kind=draft.kind.value,
            state=BreakState.OPEN.value,
            fingerprint=draft.fingerprint,
            subject={**draft.subject, **{k: str(v) for k, v in draft.extra.items()}},
            internal=None if draft.internal is None else dict(draft.internal),
            external=None if draft.external is None else dict(draft.external),
            currency=draft.currency,
            difference=draft.difference,
            payment_intent_id=draft.payment_intent_id,
            provider_payment_id=draft.provider_payment_id,
            account_id=draft.account_id,
            history=[dict(history_entry)],
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(
            index_elements=[_break.c.project_id, _break.c.fingerprint],
            index_where=_break.c.state.in_(_ACTIVE),
        )
        .returning(*_break.c)
    )
    row = (await session.execute(statement)).first()
    if row is not None:
        return _to_break(row), True
    refresh = (
        update(_break)
        .where(
            _break.c.project_id == project_id,
            _break.c.fingerprint == draft.fingerprint,
            _break.c.state.in_(_ACTIVE),
        )
        .values(
            last_run_id=run_id,
            internal=None if draft.internal is None else dict(draft.internal),
            external=None if draft.external is None else dict(draft.external),
            difference=draft.difference,
            updated_at=now,
        )
        .returning(*_break.c)
    )
    return _to_break((await session.execute(refresh)).one()), False


async def get_break(
    session: AsyncSession, project_id: UUID, break_id: UUID, *, for_update: bool = False
) -> Break | None:
    statement = select(_break).where(_break.c.project_id == project_id, _break.c.id == break_id)
    if for_update:
        statement = statement.with_for_update()
    row = (await session.execute(statement)).first()
    return None if row is None else _to_break(row)


async def list_breaks(
    session: AsyncSession,
    project_id: UUID,
    *,
    kind: BreakKind | None,
    state: BreakState | None,
    run_id: UUID | None,
    cursor: Cursor | None,
    limit: int,
) -> Sequence[Break]:
    """Newest first; ``run_id`` matches the run that opened *or* last saw the break."""
    statement = select(_break).where(_break.c.project_id == project_id)
    if kind is not None:
        statement = statement.where(_break.c.kind == kind.value)
    if state is not None:
        statement = statement.where(_break.c.state == state.value)
    if run_id is not None:
        statement = statement.where((_break.c.run_id == run_id) | (_break.c.last_run_id == run_id))
    if cursor is not None:
        statement = statement.where(keyset_predicate(_break.c.created_at, _break.c.id, cursor))
    statement = statement.order_by(_break.c.created_at.desc(), _break.c.id.desc()).limit(limit)
    return [_to_break(row) for row in (await session.execute(statement)).all()]


async def transition_break(
    session: AsyncSession,
    current: Break,
    target: BreakState,
    *,
    history_entry: Mapping[str, object],
    resolution: str | None,
    resolved_by: str | None,
    now: datetime,
) -> Break | None:
    """Move ``current`` to ``target`` if it is still in ``current.state`` (else ``None``)."""
    closing = target not in ACTIVE_STATES
    values: dict[str, object] = {
        "state": target.value,
        "history": [*(dict(item) for item in current.history), dict(history_entry)],
        "updated_at": now,
    }
    if closing:
        values |= {"resolution": resolution, "resolved_by": resolved_by, "resolved_at": now}
    statement = (
        update(_break)
        .where(
            _break.c.project_id == current.project_id,
            _break.c.id == current.id,
            _break.c.state == current.state.value,
        )
        .values(values)
        .returning(*_break.c)
    )
    row = (await session.execute(statement)).first()
    return None if row is None else _to_break(row)


@dataclass(frozen=True, slots=True)
class ActiveStats:
    """Active (open + investigating) breaks of every project, for the gauges."""

    by_kind: Mapping[str, int]
    ledger_drift_minor_units: int


async def active_stats(session: AsyncSession) -> ActiveStats:
    """Counts by kind, and the sum of ``|difference|`` of active ledger/balance drifts."""
    counts = select(_break.c.kind, func.count()).where(_break.c.state.in_(_ACTIVE))
    rows = (await session.execute(counts.group_by(_break.c.kind))).all()
    drift = select(func.coalesce(func.sum(func.abs(_break.c.difference)), 0)).where(
        _break.c.state.in_(_ACTIVE),
        _break.c.kind.in_([BreakKind.LEDGER_DRIFT.value, BreakKind.BALANCE_DRIFT.value]),
    )
    total = (await session.execute(drift)).scalar_one()
    return ActiveStats({row[0]: int(row[1]) for row in rows}, int(total))


# --- adjustments --------------------------------------------------------------------------------


async def insert_adjustment(session: AsyncSession, adjustment: Adjustment) -> bool:
    """``False`` when the break already has a proposed adjustment (one at a time)."""
    statement = (
        insert(_adjustment)
        .values(
            id=adjustment.id,
            project_id=adjustment.project_id,
            break_id=adjustment.break_id,
            provider_account_id=adjustment.provider_account_id,
            currency=adjustment.currency,
            amount=adjustment.amount,
            reason=adjustment.reason,
            payload_hash=adjustment.payload_hash,
            status=adjustment.status.value,
            proposed_by=adjustment.proposed_by,
            created_at=adjustment.created_at,
        )
        .on_conflict_do_nothing(
            index_elements=[_adjustment.c.break_id],
            index_where=_adjustment.c.status == AdjustmentStatus.PROPOSED.value,
        )
        .returning(_adjustment.c.id)
    )
    return (await session.execute(statement)).first() is not None


async def get_adjustment(
    session: AsyncSession, project_id: UUID, adjustment_id: UUID, *, for_update: bool = False
) -> Adjustment | None:
    statement = select(_adjustment).where(
        _adjustment.c.project_id == project_id, _adjustment.c.id == adjustment_id
    )
    if for_update:
        statement = statement.with_for_update()
    row = (await session.execute(statement)).first()
    return None if row is None else _to_adjustment(row)


async def list_adjustments(
    session: AsyncSession,
    project_id: UUID,
    *,
    break_id: UUID | None,
    status: AdjustmentStatus | None,
    cursor: Cursor | None,
    limit: int,
) -> Sequence[Adjustment]:
    statement = select(_adjustment).where(_adjustment.c.project_id == project_id)
    if break_id is not None:
        statement = statement.where(_adjustment.c.break_id == break_id)
    if status is not None:
        statement = statement.where(_adjustment.c.status == status.value)
    if cursor is not None:
        statement = statement.where(
            keyset_predicate(_adjustment.c.created_at, _adjustment.c.id, cursor)
        )
    statement = statement.order_by(_adjustment.c.created_at.desc(), _adjustment.c.id.desc()).limit(
        limit
    )
    return [_to_adjustment(row) for row in (await session.execute(statement)).all()]


async def decide_adjustment(
    session: AsyncSession,
    adjustment: Adjustment,
    *,
    status: AdjustmentStatus,
    decided_by: UUID,
    decision_reason: str | None,
    journal_entry_id: UUID | None,
    now: datetime,
) -> Adjustment | None:
    """Approve or reject a still-proposed adjustment (else ``None``)."""
    statement = (
        update(_adjustment)
        .where(
            _adjustment.c.id == adjustment.id,
            _adjustment.c.status == AdjustmentStatus.PROPOSED.value,
        )
        .values(
            status=status.value,
            decided_by=decided_by,
            decision_reason=decision_reason,
            journal_entry_id=journal_entry_id,
            decided_at=now,
        )
        .returning(*_adjustment.c)
    )
    row = (await session.execute(statement)).first()
    return None if row is None else _to_adjustment(row)
