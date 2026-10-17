"""Reconciliation use cases (ADR-0013, docs/RECONCILIATION.md): runs, breaks, adjustments.

A **run** is idempotent by ``(project, kind, provider account, window)``: asking again returns the
stored run; only a failed (or stale) run is executed again. Its three phases never mix a provider
call with an open transaction:

1. read the hub side (one unit of work) and build the provider client;
2. call the provider (``GET /payments``, ``GET /payments/{id}``, ``GET /finance/balance``);
3. write the breaks, the run and the events (one unit of work).

A **break** is unique while active per ``(project, fingerprint)``; a re-detection refreshes the
active one. After the run commits, ``status_mismatch`` and ``fee_mismatch`` are auto-resolved by
re-applying the provider payment through the normal payment transitions (a missed webhook, R2a);
the break is closed only if a fresh comparison agrees.

An **adjustment** (R12) is the only way a run leads to money moving in the ledger, and only with
four eyes: an operator proposes (reason and break mandatory), a *different* operator approves the
exact payload (its SHA-256 hash), and only then the entry is posted, in the approval transaction.
A rejection posts nothing. There is no step-up authentication in the hub yet: both steps require an
interactive operator session (API keys cannot propose or approve) - see SECURITY.md.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Final
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from financial_hub.modules.audit.application import (
    Actor,
    AuditContext,
    AuditRecorder,
    AuditTarget,
)
from financial_hub.modules.events.application import EventDraft, EventPublisher
from financial_hub.modules.ledger.application import (
    Approval,
    LedgerReconciliationReader,
    ProviderAccountId,
    RecipeContext,
    ReconciliationAdjustment,
    apply_recipe,
)
from financial_hub.modules.payments.application.api import (
    ExternalPayment,
    InternalPayment,
    PaymentsReconciliation,
    ProviderReadError,
    ReconciliationTarget,
)
from financial_hub.modules.reconciliation.application import events as reconciliation_events
from financial_hub.modules.reconciliation.application.metrics import (
    ReconciliationMetrics,
    shared_metrics,
)
from financial_hub.modules.reconciliation.domain import (
    ACTIVE_STATES,
    AUTO_RESOLVABLE,
    Adjustment,
    AdjustmentStatus,
    Break,
    BreakDraft,
    BreakKind,
    BreakState,
    ExternalRecord,
    InternalRecord,
    Run,
    RunKind,
    RunStatus,
    Window,
    adjustment_payload_hash,
    check_reason,
    check_transition,
    compare,
    fingerprint,
    match,
    missing_external,
    missing_internal,
)
from financial_hub.modules.reconciliation.infrastructure import repository
from financial_hub.platform.db import UnitOfWork
from financial_hub.platform.http.auth import ProjectScope
from financial_hub.shared.clock import Clock
from financial_hub.shared.errors import (
    Conflict,
    FieldError,
    Forbidden,
    NotFound,
    ValidationFailed,
)
from financial_hub.shared.ids import IdPrefix, InvalidIdError, format_id, new_uuid, parse_id
from financial_hub.shared.money import Currency, Money, UnknownCurrencyError
from financial_hub.shared.pagination import Cursor, Page, PageParams, build_page

_EVENT_TYPES = reconciliation_events  # importing the module registers the event types
_log = structlog.get_logger(__name__)

OPERATOR_PRINCIPAL: Final = "operator"
SYSTEM_ACTOR: Final = "system"
# Asaas' calendar. Brazil has had no daylight saving time since 2019: a fixed offset is exact and
# needs no tz database in the image.
PROVIDER_TZ: Final = timezone(timedelta(hours=-3), "America/Sao_Paulo")
MAX_ADJUSTMENT_MINOR: Final = 1_000_000_000_000
AUTO_RESOLUTION: Final = "auto: the provider payment was re-applied and now matches"


class AdjustmentPending(Conflict):
    slug = "adjustment-pending"
    title = "The break already has an adjustment awaiting approval."


class AdjustmentDecided(Conflict):
    slug = "adjustment-decided"
    title = "The adjustment was already approved or rejected."


class AdjustmentPayloadMismatch(Conflict):
    slug = "adjustment-payload-mismatch"
    title = "The adjustment is not the one that was reviewed."


class BreakClosed(Conflict):
    slug = "break-closed"
    title = "The break is no longer open."


class FourEyesRequired(Forbidden):
    slug = "four-eyes-required"
    title = "A different operator must decide on this adjustment."


def _invalid(pointer: str, detail: str, code: str = "invalid") -> ValidationFailed:
    return ValidationFailed(
        "The request contains invalid fields.",
        errors=[FieldError(pointer=pointer, detail=detail, code=code)],
    )


def day_bounds(window: Window) -> tuple[datetime, datetime]:
    """``[start 00:00, end + 1 00:00)`` in the provider's calendar, as aware datetimes."""
    start = datetime.combine(window.start, time(0), PROVIDER_TZ)
    until = datetime.combine(window.end + timedelta(days=1), time(0), PROVIDER_TZ)
    return start, until


def provider_today(now: datetime) -> date:
    return now.astimezone(PROVIDER_TZ).date()


def _internal_record(payment: InternalPayment) -> InternalRecord:
    return InternalRecord(
        intent_id=payment.intent_id,
        external_reference=payment.external_reference,
        provider_payment_id=payment.provider_payment_id,
        currency=payment.currency,
        amount=payment.amount,
        capture_status=payment.capture_status,
        settlement_status=payment.settlement_status,
        dispute_status=payment.dispute_status,
        booked_net=payment.booked_net,
        refunded=payment.refunded,
    )


def _external_record(payment: ExternalPayment) -> ExternalRecord:
    return ExternalRecord(
        provider_payment_id=payment.provider_payment_id,
        external_reference=payment.external_reference,
        currency=payment.currency,
        amount=payment.amount,
        capture_status=payment.capture_status,
        settlement_status=payment.settlement_status,
        dispute_status=payment.dispute_status,
        net_amount=payment.net_amount,
        refunded=payment.refunded,
    )


def _with_account(draft: BreakDraft, ledger_account: UUID) -> BreakDraft:
    """Tag a provider-side break with the ``provider_balance`` owner an R12 would adjust."""
    return BreakDraft(
        draft.kind,
        draft.fingerprint,
        subject=draft.subject,
        internal=draft.internal,
        external=draft.external,
        currency=draft.currency,
        difference=draft.difference,
        payment_intent_id=draft.payment_intent_id,
        provider_payment_id=draft.provider_payment_id,
        account_id=draft.account_id,
        extra={
            **draft.extra,
            "provider_account_id": format_id(IdPrefix.PROVIDER_ACCOUNT, ledger_account),
        },
    )


@dataclass(frozen=True, slots=True)
class _Detection:
    drafts: list[BreakDraft]
    summary: dict[str, object]


class ReconciliationService:
    """Opens its own units of work (provider calls happen between them)."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Clock,
        payments: PaymentsReconciliation,
        audit: AuditContext | None = None,
        publisher: EventPublisher | None = None,
        metrics: ReconciliationMetrics | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._payments = payments
        self._audit = audit or AuditContext.system()
        self._publisher = publisher or EventPublisher(clock=clock, notifier=None)
        self._metrics = metrics or shared_metrics()

    def _uow(self) -> UnitOfWork:
        return UnitOfWork(self._session_factory)

    async def _record(
        self,
        session: AsyncSession,
        scope: ProjectScope,
        action: str,
        target: AuditTarget,
        diff: dict[str, Any],
    ) -> None:
        await AuditRecorder.record(
            session,
            actor=self._audit.acting(Actor.of_principal(scope.principal_kind, scope.principal_id)),
            action=action,
            target=target,
            diff=diff,
            project_id=scope.project_id,
        )

    # --- runs -----------------------------------------------------------------------------------

    async def start_run(
        self,
        scope: ProjectScope,
        *,
        kind: str,
        window_start: date | None,
        window_end: date | None,
        provider_account_id: str | None = None,
    ) -> Run:
        """Run (or return) the reconciliation of one window; default window: yesterday."""
        try:
            run_kind = RunKind(kind)
        except ValueError:
            raise _invalid("/kind", "kind must be internal, transactions or balance") from None
        window = self._window(window_start, window_end)
        target: ReconciliationTarget | None = None
        if run_kind is not RunKind.INTERNAL:
            target = await self._target(scope.project_id, provider_account_id)
        run = await self.run(
            scope.project_id, run_kind, window, target=target, triggered_by=scope.principal_id
        )
        async with self._uow() as uow:
            await self._record(
                uow.session,
                scope,
                "reconciliation.run_requested",
                AuditTarget("reconciliation_run", format_id(IdPrefix.RECONCILIATION_RUN, run.id)),
                {"kind": run.kind.value, "status": run.status.value},
            )
        return run

    def _window(self, start: date | None, end: date | None) -> Window:
        if start is None and end is None:
            return Window.day_before(provider_today(self._clock.now()))
        if start is None or end is None:
            raise _invalid("/window_start", "give both window_start and window_end, or neither")
        window = Window(start, end)
        if window.end >= provider_today(self._clock.now()):
            raise _invalid("/window_end", "the window must end before today (America/Sao_Paulo)")
        return window

    async def _target(self, project_id: UUID, raw_account: str | None) -> ReconciliationTarget:
        async with self._uow() as uow:
            targets = [
                target
                for target in await PaymentsReconciliation.targets(uow.session)
                if target.project_id == project_id
            ]
        if raw_account is not None:
            try:
                account_id = parse_id(raw_account, IdPrefix.PROVIDER_ACCOUNT)
            except InvalidIdError:
                raise _invalid("/provider_account_id", "not a pva_ id") from None
            targets = [target for target in targets if target.provider_account_id == account_id]
        if not targets:
            raise _invalid("/provider_account_id", "the project has no payments on this account")
        if len(targets) > 1:
            raise _invalid(
                "/provider_account_id", "the project has several accounts: name one", "required"
            )
        return targets[0]

    async def run(
        self,
        project_id: UUID,
        kind: RunKind,
        window: Window,
        *,
        target: ReconciliationTarget | None,
        triggered_by: str,
    ) -> Run:
        """Claim the run of this scope and execute it if this caller owns it."""
        provider = "" if target is None else target.provider
        account = None if target is None else target.provider_account_id
        async with self._uow() as uow:
            claim = await repository.claim_run(
                uow.session,
                run_id=new_uuid(),
                project_id=project_id,
                kind=kind,
                provider=provider,
                provider_account_id=account,
                window=window,
                triggered_by=triggered_by,
                now=self._clock.now(),
            )
        if not claim.claimed:
            return claim.run
        run = claim.run
        try:
            match kind:
                case RunKind.INTERNAL:
                    detection = await self._detect_internal(project_id, window)
                case RunKind.TRANSACTIONS:
                    assert target is not None  # resolved by start_run / run_scheduled  # noqa: S101
                    detection = await self._detect_transactions(target, window)
                case RunKind.BALANCE:
                    assert target is not None  # resolved by start_run / run_scheduled  # noqa: S101
                    detection = await self._detect_balance(target)
        except ProviderReadError as error:
            _log.warning("reconciliation.run_failed", run_id=str(run.id), code=error.code)
            return await self._fail(run, error.code)
        except Exception:
            _log.exception("reconciliation.run_crashed", run_id=str(run.id))
            await self._fail(run, "internal_error")
            raise
        stored, candidates = await self._complete(run, detection)
        if target is not None and candidates:
            await self._auto_resolve(target, candidates)
        await self.refresh_metrics()
        return stored

    async def record_demo_fee_mismatch(
        self, project_id: UUID, *, day: date, provider_payment_id: str, extra_fee: int
    ) -> tuple[Run, bool]:
        """``seed-demo`` only: a completed transactions run with one open ``fee_mismatch``.

        On the project's fake-provider payments. The fake provider keeps no fee history, so the
        provider side is scripted: the hub's payment as booked, with a net ``extra_fee`` minor
        units lower. Idempotent by the run scope: ``(run, created)``, and a second call returns
        the stored run. No auto-resolution runs and nothing is posted.
        """
        if extra_fee <= 0:
            raise ValueError("extra_fee must be positive")
        async with self._uow() as uow:
            targets = [
                item
                for item in await PaymentsReconciliation.targets(uow.session)
                if item.project_id == project_id and item.provider_account_id is None
            ]
            found = (
                await PaymentsReconciliation.internal_by_provider_ids(
                    uow.session, targets[0], [provider_payment_id]
                )
                if targets
                else []
            )
        if not found or found[0].booked_net is None:
            raise NotFound("No booked fake-provider payment to script a fee mismatch on.")
        target = targets[0]
        window = Window(day, day)
        hub_record = _internal_record(found[0])
        provider_record = ExternalRecord(
            provider_payment_id=hub_record.provider_payment_id,
            external_reference=hub_record.external_reference,
            currency=hub_record.currency,
            amount=hub_record.amount,
            capture_status=hub_record.capture_status,
            settlement_status=hub_record.settlement_status,
            dispute_status=hub_record.dispute_status,
            net_amount=found[0].booked_net - extra_fee,
            refunded=hub_record.refunded,
        )
        async with self._uow() as uow:
            claim = await repository.claim_run(
                uow.session,
                run_id=new_uuid(),
                project_id=target.project_id,
                kind=RunKind.TRANSACTIONS,
                provider=target.provider,
                provider_account_id=target.provider_account_id,
                window=window,
                triggered_by="seed-demo",
                now=self._clock.now(),
            )
        if not claim.claimed:
            return claim.run, False
        ledger_account = PaymentsReconciliation.ledger_provider_account(
            target.project_id, target.provider, target.provider_account_id
        )
        drafts = [
            _with_account(draft, ledger_account) for draft in compare(hub_record, provider_record)
        ]
        summary: dict[str, object] = {"internal": 1, "external": 1, "matched": 1, "demo": True}
        stored, _ = await self._complete(claim.run, _Detection(drafts, summary))
        await self.refresh_metrics()
        return stored, True

    async def _fail(self, run: Run, code: str) -> Run:
        async with self._uow() as uow:
            failed = await repository.finish_run(
                uow.session,
                run.id,
                status=RunStatus.FAILED,
                summary={},
                breaks_opened=0,
                error_code=code,
                now=self._clock.now(),
            )
            await self._run_event(uow, failed)
        return failed

    async def _run_event(self, uow: UnitOfWork, run: Run) -> None:
        run_ref = format_id(IdPrefix.RECONCILIATION_RUN, run.id)
        await self._publisher.publish(
            uow,
            EventDraft(
                type=reconciliation_events.RUN_COMPLETED.name,
                data=reconciliation_events.RunCompletedPayload(
                    run_id=run_ref,
                    kind=run.kind.value,
                    status=run.status.value,
                    window_start=run.window.start.isoformat(),
                    window_end=run.window.end.isoformat(),
                    breaks_opened=run.breaks_opened,
                ),
                project_id=run.project_id,
                subject=run_ref,
            ),
        )

    async def _complete(self, run: Run, detection: _Detection) -> tuple[Run, list[Break]]:
        """Store the breaks and close the run; returns the active auto-resolvable breaks."""
        now = self._clock.now()
        run_ref = format_id(IdPrefix.RECONCILIATION_RUN, run.id)
        opened = 0
        candidates: list[Break] = []
        by_kind: dict[str, int] = {}
        async with self._uow() as uow:
            for draft in detection.drafts:
                stored, created = await repository.open_break(
                    uow.session,
                    break_id=new_uuid(),
                    project_id=run.project_id,
                    run_id=run.id,
                    draft=draft,
                    history_entry={
                        "at": now.isoformat(),
                        "by": SYSTEM_ACTOR,
                        "to": "open",
                        "run_id": run_ref,
                    },
                    now=now,
                )
                by_kind[draft.kind.value] = by_kind.get(draft.kind.value, 0) + 1
                if stored.kind in AUTO_RESOLVABLE:
                    candidates.append(stored)
                if created:
                    opened += 1
                    break_ref = format_id(IdPrefix.RECONCILIATION_BREAK, stored.id)
                    await self._publisher.publish(
                        uow,
                        EventDraft(
                            type=reconciliation_events.BREAK_OPENED.name,
                            data=reconciliation_events.BreakOpenedPayload(
                                break_id=break_ref, run_id=run_ref, kind=stored.kind.value
                            ),
                            project_id=run.project_id,
                            subject=break_ref,
                        ),
                    )
            completed = await repository.finish_run(
                uow.session,
                run.id,
                status=RunStatus.COMPLETED,
                summary={**detection.summary, "breaks": by_kind},
                breaks_opened=opened,
                error_code=None,
                now=now,
            )
            await self._run_event(uow, completed)
        return completed, candidates

    # --- detection ------------------------------------------------------------------------------

    async def _detect_internal(self, project_id: UUID, window: Window) -> _Detection:
        drafts: list[BreakDraft] = []
        start, until = day_bounds(window)
        async with self._uow() as uow:
            ledger = LedgerReconciliationReader(uow)
            drifts = await ledger.drifted_accounts(project_id)
            for drift in drifts:
                materialized, recomputed = drift.materialized, drift.recomputed
                difference = (recomputed.posted_dr - recomputed.posted_cr) - (
                    materialized.posted_dr - materialized.posted_cr
                ) or (recomputed.pending_dr - recomputed.pending_cr) - (
                    materialized.pending_dr - materialized.pending_cr
                )
                drafts.append(
                    BreakDraft(
                        BreakKind.LEDGER_DRIFT,
                        fingerprint(BreakKind.LEDGER_DRIFT, drift.account_id),
                        subject={"account_id": str(drift.account_id), "code": drift.code},
                        internal={"source": "materialized", **materialized.as_dict()},
                        external={"source": "postings", **recomputed.as_dict()},
                        currency=drift.currency,
                        difference=difference,
                        account_id=drift.account_id,
                    )
                )
            trials = await ledger.trial_balances(project_id)
            for trial in trials:
                if trial.posted_net == 0 and trial.pending_net == 0:
                    continue
                drafts.append(
                    BreakDraft(
                        BreakKind.LEDGER_DRIFT,
                        fingerprint(BreakKind.LEDGER_DRIFT, "trial", trial.currency),
                        subject={"scope": "trial_balance", "currency": trial.currency},
                        internal={"source": "trial_balance", **trial.totals.as_dict()},
                        external={"expected_net_minor": 0},
                        currency=trial.currency,
                        difference=trial.posted_net or trial.pending_net,
                    )
                )
            intents: list[UUID] = []
            for target in await PaymentsReconciliation.targets(uow.session):
                if target.project_id != project_id:
                    continue
                found = await PaymentsReconciliation.internal_in_window(
                    uow.session, target, start, until
                )
                intents.extend(payment.intent_id for payment in found)
            settled = await PaymentsReconciliation.settled_standalone_intents(
                uow.session, project_id, intents
            )
            residues = await ledger.clearing_residues(project_id, settled)
            for residue in residues:
                intent_ref = format_id(IdPrefix.PAYMENT_INTENT, residue.correlation_id)
                drafts.append(
                    BreakDraft(
                        BreakKind.CLEARING_NOT_ZERO,
                        fingerprint(
                            BreakKind.CLEARING_NOT_ZERO, residue.correlation_id, residue.code
                        ),
                        subject={"payment_intent_id": intent_ref, "code": residue.code},
                        internal={"code": residue.code, "net_debit_minor": residue.net_debit},
                        external={"expected_net_minor": 0},
                        currency=residue.currency,
                        difference=residue.net_debit,
                        payment_intent_id=residue.correlation_id,
                    )
                )
        summary: dict[str, object] = {
            "accounts_drifted": len(drifts),
            "currencies": len(trials),
            "intents_checked": len(settled),
        }
        return _Detection(drafts, summary)

    async def _detect_transactions(
        self, target: ReconciliationTarget, window: Window
    ) -> _Detection:
        start, until = day_bounds(window)
        async with self._uow() as uow:
            reader = await self._payments.reader(uow.session, target)
            hub = [
                _internal_record(payment)
                for payment in await PaymentsReconciliation.internal_in_window(
                    uow.session, target, start, until
                )
            ]
        listed = [
            _external_record(payment)
            for payment in await reader.list_payments(window.start, window.end)
        ]
        result = match(hub, listed)
        drafts: list[BreakDraft] = []
        for hub_record, provider_record in result.pairs:
            drafts.extend(compare(hub_record, provider_record))
        # Listed but not in the window's hub payments: the hub may know them from another day.
        if result.external_only:
            async with self._uow() as uow:
                known = await PaymentsReconciliation.internal_by_provider_ids(
                    uow.session, target, [item.provider_payment_id for item in result.external_only]
                )
                references = [
                    item.external_reference
                    for item in result.external_only
                    if item.external_reference is not None
                ]
                known += await PaymentsReconciliation.internal_by_references(
                    uow.session, target, references
                )
            late = match([_internal_record(payment) for payment in known], result.external_only)
            for hub_record, provider_record in late.pairs:
                drafts.extend(compare(hub_record, provider_record))
            drafts.extend(missing_internal(item) for item in late.external_only)
        # In the window but not listed: the provider is asked by id before calling it missing.
        for hub_record in result.internal_only:
            provider_record = await reader.get_payment(hub_record.provider_payment_id)
            if provider_record is None:
                drafts.append(missing_external(hub_record))
            else:
                drafts.extend(compare(hub_record, _external_record(provider_record)))
        ledger_account = PaymentsReconciliation.ledger_provider_account(
            target.project_id, target.provider, target.provider_account_id
        )
        summary: dict[str, object] = {
            "internal": len(hub),
            "external": len(listed),
            "matched": len(result.pairs),
        }
        return _Detection([_with_account(draft, ledger_account) for draft in drafts], summary)

    async def _detect_balance(self, target: ReconciliationTarget) -> _Detection:
        async with self._uow() as uow:
            reader = await self._payments.reader(uow.session, target)
        balance = await reader.get_balance()
        ledger_account = PaymentsReconciliation.ledger_provider_account(
            target.project_id, target.provider, target.provider_account_id
        )
        async with self._uow() as uow:
            available, held = await LedgerReconciliationReader(uow).provider_balance(
                target.project_id, ledger_account, Currency.of(balance.currency)
            )
        booked = available + held
        summary: dict[str, object] = {
            "currency": balance.currency,
            "provider_minor": balance.amount,
            "booked_minor": booked,
        }
        if balance.amount == booked:
            return _Detection([], summary)
        draft = BreakDraft(
            BreakKind.BALANCE_DRIFT,
            fingerprint(BreakKind.BALANCE_DRIFT, ledger_account, balance.currency),
            subject={"provider": target.provider, "currency": balance.currency},
            internal={
                "provider_balance_minor": available,
                "provider_balance_held_minor": held,
                "total_minor": booked,
            },
            external={"balance_minor": balance.amount},
            currency=balance.currency,
            difference=balance.amount - booked,
            extra={"provider_account_id": format_id(IdPrefix.PROVIDER_ACCOUNT, ledger_account)},
        )
        return _Detection([draft], summary)

    # --- auto-resolution ------------------------------------------------------------------------

    async def _auto_resolve(self, target: ReconciliationTarget, candidates: list[Break]) -> None:
        """Re-apply each payment, compare afresh, close the break if the difference is gone.

        Best effort: any failure leaves the break open for an operator.
        """
        async with self._uow() as uow:
            reader = await self._payments.reader(uow.session, target)
        refreshed: set[UUID] = set()
        for candidate in candidates:
            intent_id, pp_id = candidate.payment_intent_id, candidate.provider_payment_id
            if intent_id is None or pp_id is None:
                continue
            try:
                if intent_id not in refreshed:
                    await self._payments.refresh(intent_id)
                    refreshed.add(intent_id)
                provider_record = await reader.get_payment(pp_id)
            except Exception:
                _log.warning("reconciliation.auto_resolve_failed", break_id=str(candidate.id))
                continue
            if provider_record is None:
                continue
            async with self._uow() as uow:
                hub = await PaymentsReconciliation.internal_by_provider_ids(
                    uow.session, target, [pp_id]
                )
                if len(hub) != 1:
                    continue
                remaining = compare(_internal_record(hub[0]), _external_record(provider_record))
                if any(draft.fingerprint == candidate.fingerprint for draft in remaining):
                    continue
                current = await repository.get_break(
                    uow.session, candidate.project_id, candidate.id, for_update=True
                )
                if current is None or current.state not in ACTIVE_STATES:
                    continue
                await self._close(
                    uow,
                    current,
                    BreakState.RESOLVED,
                    by=SYSTEM_ACTOR,
                    reason=AUTO_RESOLUTION,
                    automatic=True,
                )

    async def _close(
        self,
        uow: UnitOfWork,
        current: Break,
        target: BreakState,
        *,
        by: str,
        reason: str | None,
        automatic: bool = False,
    ) -> Break:
        now = self._clock.now()
        moved = await repository.transition_break(
            uow.session,
            current,
            target,
            history_entry={
                "at": now.isoformat(),
                "by": by,
                "from": current.state.value,
                "to": target.value,
                "reason": reason,
            },
            resolution=reason,
            resolved_by=by,
            now=now,
        )
        if moved is None:
            raise BreakClosed("The break changed meanwhile; reload it.")
        if target not in ACTIVE_STATES:
            break_ref = format_id(IdPrefix.RECONCILIATION_BREAK, moved.id)
            await self._publisher.publish(
                uow,
                EventDraft(
                    type=reconciliation_events.BREAK_RESOLVED.name,
                    data=reconciliation_events.BreakResolvedPayload(
                        break_id=break_ref,
                        kind=moved.kind.value,
                        state=moved.state.value,
                        automatic=automatic,
                    ),
                    project_id=moved.project_id,
                    subject=break_ref,
                ),
            )
        return moved

    async def refresh_metrics(self) -> None:
        async with self._uow() as uow:
            stats = await repository.active_stats(uow.session)
        self._metrics.update(stats.by_kind, stats.ledger_drift_minor_units)

    # --- reads ----------------------------------------------------------------------------------

    async def get_run(self, project_id: UUID, raw_id: str) -> Run:
        run_id = _parse(raw_id, IdPrefix.RECONCILIATION_RUN, "Run not found.")
        async with self._uow() as uow:
            run = await repository.get_run(uow.session, project_id, run_id)
        if run is None:
            raise NotFound("Run not found.")
        return run

    async def list_runs(
        self, project_id: UUID, params: PageParams, *, kind: str | None, status: str | None
    ) -> Page[Run]:
        wanted_kind = _enum(RunKind, kind, "/kind")
        wanted_status = _enum(RunStatus, status, "/status")
        async with self._uow() as uow:
            rows = await repository.list_runs(
                uow.session,
                project_id,
                kind=wanted_kind,
                status=wanted_status,
                cursor=params.decoded_cursor(),
                limit=params.limit + 1,
            )
        return build_page(rows, params.limit, lambda run: run, _cursor)

    async def get_break(self, project_id: UUID, raw_id: str) -> Break:
        break_id = _parse(raw_id, IdPrefix.RECONCILIATION_BREAK, "Break not found.")
        async with self._uow() as uow:
            found = await repository.get_break(uow.session, project_id, break_id)
        if found is None:
            raise NotFound("Break not found.")
        return found

    async def list_breaks(
        self,
        project_id: UUID,
        params: PageParams,
        *,
        kind: str | None,
        state: str | None,
        run_id: str | None,
    ) -> Page[Break]:
        wanted_kind = _enum(BreakKind, kind, "/kind")
        wanted_state = _enum(BreakState, state, "/state")
        wanted_run = None
        if run_id is not None:
            try:
                wanted_run = parse_id(run_id, IdPrefix.RECONCILIATION_RUN)
            except InvalidIdError:
                raise _invalid("/run_id", "not a rec_ id") from None
        async with self._uow() as uow:
            rows = await repository.list_breaks(
                uow.session,
                project_id,
                kind=wanted_kind,
                state=wanted_state,
                run_id=wanted_run,
                cursor=params.decoded_cursor(),
                limit=params.limit + 1,
            )
        return build_page(rows, params.limit, lambda item: item, _cursor)

    # --- break workflow -------------------------------------------------------------------------

    async def transition_break(
        self, scope: ProjectScope, raw_id: str, *, state: str, reason: str | None
    ) -> Break:
        """``investigating`` and ``accepted`` need a reason; closed breaks never reopen."""
        target = _enum(BreakState, state, "/state")
        if target is None or target is BreakState.OPEN:
            raise _invalid("/state", "state must be investigating, resolved or accepted")
        break_id = _parse(raw_id, IdPrefix.RECONCILIATION_BREAK, "Break not found.")
        async with self._uow() as uow:
            current = await repository.get_break(
                uow.session, scope.project_id, break_id, for_update=True
            )
            if current is None:
                raise NotFound("Break not found.")
            cleaned = check_transition(current.state, target, reason)
            moved = await self._close(uow, current, target, by=scope.principal_id, reason=cleaned)
            await self._record(
                uow.session,
                scope,
                "reconciliation.break_transitioned",
                AuditTarget(
                    "reconciliation_break", format_id(IdPrefix.RECONCILIATION_BREAK, moved.id)
                ),
                {"from": current.state.value, "to": moved.state.value, "reason": cleaned},
            )
        await self.refresh_metrics()
        return moved

    # --- adjustments (R12, four eyes) -----------------------------------------------------------

    async def propose_adjustment(
        self,
        scope: ProjectScope,
        raw_break_id: str,
        *,
        amount: int,
        currency: str | None,
        reason: str | None,
        provider_account_id: str | None,
    ) -> Adjustment:
        operator = _operator(scope)
        cleaned = check_reason(reason, required=True)
        assert cleaned is not None  # required=True never returns None  # noqa: S101
        if type(amount) is not int or amount == 0 or abs(amount) > MAX_ADJUSTMENT_MINOR:
            raise _invalid("/amount", "a non-zero integer of minor units within limits")
        break_id = _parse(raw_break_id, IdPrefix.RECONCILIATION_BREAK, "Break not found.")
        async with self._uow() as uow:
            current = await repository.get_break(
                uow.session, scope.project_id, break_id, for_update=True
            )
            if current is None:
                raise NotFound("Break not found.")
            if current.state not in ACTIVE_STATES:
                raise BreakClosed("Only an open or investigating break can be adjusted.")
            code = currency or current.currency
            try:
                money_currency = Currency.of(code or "")
            except UnknownCurrencyError:
                raise _invalid("/currency", "an ISO 4217 code is required") from None
            account = _adjusted_account(current, provider_account_id)
            adjustment = Adjustment(
                id=new_uuid(),
                project_id=scope.project_id,
                break_id=current.id,
                provider_account_id=account,
                currency=money_currency.code,
                amount=amount,
                reason=cleaned,
                payload_hash=adjustment_payload_hash(
                    break_id=current.id,
                    provider_account_id=account,
                    currency=money_currency.code,
                    amount=amount,
                    reason=cleaned,
                ),
                status=AdjustmentStatus.PROPOSED,
                proposed_by=operator,
                decided_by=None,
                decision_reason=None,
                journal_entry_id=None,
                created_at=self._clock.now(),
                decided_at=None,
            )
            if not await repository.insert_adjustment(uow.session, adjustment):
                raise AdjustmentPending("Approve or reject the pending adjustment first.")
            await self._record(
                uow.session,
                scope,
                "reconciliation.adjustment_proposed",
                _adjustment_target(adjustment),
                {
                    "break_id": format_id(IdPrefix.RECONCILIATION_BREAK, current.id),
                    "amount_minor": amount,
                    "currency": money_currency.code,
                    "reason": cleaned,
                    "payload_hash": adjustment.payload_hash,
                },
            )
        return adjustment

    async def approve_adjustment(
        self, scope: ProjectScope, raw_id: str, *, payload_hash: str
    ) -> Adjustment:
        """Post R12 (``provider_balance`` vs ``opening_balance``) and resolve the break."""
        approver = _operator(scope)
        adjustment_id = _parse(raw_id, IdPrefix.RECONCILIATION_ADJUSTMENT, "Adjustment not found.")
        async with self._uow() as uow:
            adjustment = await self._pending(uow, scope.project_id, adjustment_id, approver)
            if payload_hash != adjustment.payload_hash or payload_hash != adjustment_payload_hash(
                break_id=adjustment.break_id,
                provider_account_id=adjustment.provider_account_id,
                currency=adjustment.currency,
                amount=adjustment.amount,
                reason=adjustment.reason,
            ):
                raise AdjustmentPayloadMismatch("Review the adjustment again before approving.")
            current = await repository.get_break(
                uow.session, scope.project_id, adjustment.break_id, for_update=True
            )
            if current is None or current.state not in ACTIVE_STATES:
                raise BreakClosed("The break was closed; reject this adjustment instead.")
            now = self._clock.now()
            result = await apply_recipe(
                uow,
                ReconciliationAdjustment(
                    adjustment_id=adjustment.id,
                    provider_account_id=ProviderAccountId(adjustment.provider_account_id),
                    amount=Money(adjustment.amount, Currency.of(adjustment.currency)),
                    # Entry metadata takes a code, never free text (PII): the break kind.
                    # The operator's reason stays on the adjustment row and in the audit.
                    reason=f"reconciliation.{current.kind.value}",
                    approval=Approval(
                        approval_id=adjustment.id,
                        requested_by=adjustment.proposed_by,
                        approved_by=approver,
                    ),
                ),
                context=RecipeContext(
                    project_id=scope.project_id,
                    effective_at=now,
                    correlation_id=adjustment.id,
                    causation_id=current.id,
                ),
            )
            entry_id = result.entries[0].id
            decided = await repository.decide_adjustment(
                uow.session,
                adjustment,
                status=AdjustmentStatus.APPROVED,
                decided_by=approver,
                decision_reason=None,
                journal_entry_id=entry_id,
                now=now,
            )
            if decided is None:
                raise AdjustmentDecided("The adjustment was decided meanwhile.")
            adjustment_ref = format_id(IdPrefix.RECONCILIATION_ADJUSTMENT, adjustment.id)
            await self._close(
                uow,
                current,
                BreakState.RESOLVED,
                by=scope.principal_id,
                reason=f"adjusted by {adjustment_ref}",
            )
            await self._record(
                uow.session,
                scope,
                "reconciliation.adjustment_approved",
                _adjustment_target(decided),
                {
                    "journal_entry_id": format_id(IdPrefix.JOURNAL_ENTRY, entry_id),
                    "payload_hash": payload_hash,
                },
            )
        await self.refresh_metrics()
        return decided

    async def reject_adjustment(
        self, scope: ProjectScope, raw_id: str, *, reason: str | None
    ) -> Adjustment:
        """Nothing is posted; the break stays as it was."""
        decider = _operator(scope)
        cleaned = check_reason(reason, required=True)
        adjustment_id = _parse(raw_id, IdPrefix.RECONCILIATION_ADJUSTMENT, "Adjustment not found.")
        async with self._uow() as uow:
            adjustment = await self._pending(uow, scope.project_id, adjustment_id, decider)
            decided = await repository.decide_adjustment(
                uow.session,
                adjustment,
                status=AdjustmentStatus.REJECTED,
                decided_by=decider,
                decision_reason=cleaned,
                journal_entry_id=None,
                now=self._clock.now(),
            )
            if decided is None:
                raise AdjustmentDecided("The adjustment was decided meanwhile.")
            await self._record(
                uow.session,
                scope,
                "reconciliation.adjustment_rejected",
                _adjustment_target(decided),
                {"reason": cleaned},
            )
        return decided

    async def _pending(
        self, uow: UnitOfWork, project_id: UUID, adjustment_id: UUID, decider: UUID
    ) -> Adjustment:
        adjustment = await repository.get_adjustment(
            uow.session, project_id, adjustment_id, for_update=True
        )
        if adjustment is None:
            raise NotFound("Adjustment not found.")
        if adjustment.status is not AdjustmentStatus.PROPOSED:
            raise AdjustmentDecided("The adjustment was already decided.")
        if adjustment.proposed_by == decider:
            raise FourEyesRequired("The operator who proposed an adjustment cannot decide it.")
        return adjustment

    async def get_adjustment(self, project_id: UUID, raw_id: str) -> Adjustment:
        adjustment_id = _parse(raw_id, IdPrefix.RECONCILIATION_ADJUSTMENT, "Adjustment not found.")
        async with self._uow() as uow:
            found = await repository.get_adjustment(uow.session, project_id, adjustment_id)
        if found is None:
            raise NotFound("Adjustment not found.")
        return found

    async def list_adjustments(
        self,
        project_id: UUID,
        params: PageParams,
        *,
        break_id: str | None,
        status: str | None,
    ) -> Page[Adjustment]:
        wanted_status = _enum(AdjustmentStatus, status, "/status")
        wanted_break = None
        if break_id is not None:
            try:
                wanted_break = parse_id(break_id, IdPrefix.RECONCILIATION_BREAK)
            except InvalidIdError:
                raise _invalid("/break_id", "not a rbk_ id") from None
        async with self._uow() as uow:
            rows = await repository.list_adjustments(
                uow.session,
                project_id,
                break_id=wanted_break,
                status=wanted_status,
                cursor=params.decoded_cursor(),
                limit=params.limit + 1,
            )
        return build_page(rows, params.limit, lambda item: item, _cursor)

    # --- scheduler ------------------------------------------------------------------------------

    async def run_scheduled(self) -> list[Run]:
        """The previous day of every project and account: internal, transactions, balance."""
        window = Window.day_before(provider_today(self._clock.now()))
        async with self._uow() as uow:
            targets = await PaymentsReconciliation.targets(uow.session)
        runs: list[Run] = []
        projects = sorted({target.project_id for target in targets})
        for project_id in projects:
            runs.append(
                await self.run(
                    project_id, RunKind.INTERNAL, window, target=None, triggered_by=SYSTEM_ACTOR
                )
            )
        for target in targets:
            for kind in (RunKind.TRANSACTIONS, RunKind.BALANCE):
                try:
                    runs.append(
                        await self.run(
                            target.project_id,
                            kind,
                            window,
                            target=target,
                            triggered_by=SYSTEM_ACTOR,
                        )
                    )
                except Exception:
                    _log.exception("reconciliation.scheduled_run_crashed", kind=kind.value)
        return runs


def _cursor(item: Run | Break | Adjustment) -> Cursor:
    return Cursor(created_at=item.created_at, id=item.id)


def _parse(raw: str, prefix: IdPrefix, missing: str) -> UUID:
    try:
        return parse_id(raw, prefix)
    except InvalidIdError:
        raise NotFound(missing) from None


def _enum[E: (RunKind, RunStatus, BreakKind, BreakState, AdjustmentStatus)](
    enum: type[E], raw: str | None, pointer: str
) -> E | None:
    if raw is None:
        return None
    try:
        return enum(raw)
    except ValueError:
        allowed = ", ".join(member.value for member in enum)
        raise _invalid(pointer, f"must be one of: {allowed}") from None


def _operator(scope: ProjectScope) -> UUID:
    """Four-eyes needs two *people*: only interactive operator sessions propose or decide."""
    if scope.principal_kind != OPERATOR_PRINCIPAL:
        raise Forbidden("Adjustments are proposed and approved by operators only.")
    try:
        return parse_id(scope.principal_id, IdPrefix.OPERATOR)
    except InvalidIdError:
        raise Forbidden("Adjustments are proposed and approved by operators only.") from None


def _adjusted_account(current: Break, raw: str | None) -> UUID:
    if raw is not None:
        try:
            return parse_id(raw, IdPrefix.PROVIDER_ACCOUNT)
        except InvalidIdError:
            raise _invalid("/provider_account_id", "not a pva_ id") from None
    tagged = current.subject.get("provider_account_id")
    if isinstance(tagged, str):
        return parse_id(tagged, IdPrefix.PROVIDER_ACCOUNT)
    raise _invalid(
        "/provider_account_id", "this break names no provider account: give one", "required"
    )


def _adjustment_target(adjustment: Adjustment) -> AuditTarget:
    return AuditTarget(
        "reconciliation_adjustment", format_id(IdPrefix.RECONCILIATION_ADJUSTMENT, adjustment.id)
    )


__all__ = [
    "AdjustmentDecided",
    "AdjustmentPayloadMismatch",
    "AdjustmentPending",
    "BreakClosed",
    "FourEyesRequired",
    "ReconciliationService",
    "day_bounds",
    "provider_today",
]
