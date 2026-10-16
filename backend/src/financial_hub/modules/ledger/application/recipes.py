"""Apply posting recipes (FINANCIAL_MODEL s4) inside the caller's unit of work.

:func:`apply_recipes` turns recipe inputs into one :meth:`LedgerService.write`: accounts are
provisioned lazily (upsert-then-lock), every account of the unit of work is locked once, and the
entries are written in the caller's transaction. Nothing here commits.

Lock order (ADR-0003 s5): the business aggregate (``aggregate_lock``) first, then the stored
entries a recipe depends on are read (R5 unblock, R8/R10 second phase, R9 won/lost, R10 return,
R11), then the ledger accounts.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import assert_never, cast
from uuid import UUID

from financial_hub.modules.ledger.application.service import (
    AggregateLock,
    DeferredEntry,
    LedgerService,
    Transition,
    WriteItem,
)
from financial_hub.modules.ledger.domain.accounts import (
    PAYEE_PAYABLE_HELD,
    PLATFORM_REVENUE,
    PROVIDER_BALANCE,
    PROVIDER_BALANCE_HELD,
    PayeeId,
    template_of,
)
from financial_hub.modules.ledger.domain.model import (
    Account,
    AccountSpec,
    Balance,
    Direction,
    EntryDraft,
    EntryStatus,
    JournalEntry,
    JsonValue,
)
from financial_hub.modules.ledger.domain.recipes import (
    DisputeLost,
    DisputeOpened,
    DisputeWon,
    FeeAdjusted,
    InvoiceFinalized,
    InvoiceVoided,
    PaymentCaptured,
    PaymentSettled,
    PayoutFailed,
    PayoutPaid,
    PayoutRequested,
    PayoutReturned,
    ProviderSplitBlocked,
    ProviderSplitDone,
    ProviderSplitRefundRequested,
    ProviderSplitUnblocked,
    ReceivedInCash,
    ReceivedInCashRejected,
    RecipeCode,
    RecipeContext,
    RecipeInput,
    ReconciliationAdjustment,
    RefundCompleted,
    RefundOutcome,
    RefundRequested,
    SplitAllocated,
    TopUpCaptured,
    TopUpRefundRequested,
    UnappliedCashReceived,
    UnappliedCashRefundRequested,
    WalletConsumed,
    WriteOff,
    balance_accounts,
    build_r1,
    build_r2,
    build_r2a,
    build_r2b,
    build_r3,
    build_r4,
    build_r5_blocked,
    build_r5_done,
    build_r6,
    build_r7,
    build_r8_internal,
    build_r8_provider_split,
    build_r8_top_up,
    build_r8_unapplied,
    build_r9_lost,
    build_r9_opened,
    build_r10_fee,
    build_r10_request,
    build_r12,
    build_r13,
    dispute_opened_key,
    invoice_finalized_key,
    invoice_wallet_key,
    payment_captured_key,
    payout_pending_key,
    refund_pending_key,
    split_blocked_key,
)
from financial_hub.platform.db import UnitOfWork
from financial_hub.shared.errors import Conflict, NotFound
from financial_hub.shared.money import Currency, Money

type RefundRequestInput = (
    RefundRequested
    | ProviderSplitRefundRequested
    | TopUpRefundRequested
    | UnappliedCashRefundRequested
)


@dataclass(frozen=True, slots=True)
class RefundFinished:
    """R8 step 2 when step 1 may be missing; ``request`` carries the facts of step 1.

    - the pending entry of the refund exists: same as :class:`RefundCompleted` (post or void it);
    - no pending entry and ``DONE`` (a refund made at the provider and imported, or one requested
      before its hold was booked): step 1 is booked **directly as posted**, under the same key
      ``refund:{id}:pending``, so a later :class:`RefundCompleted` of it is a no-op;
    - no pending entry and ``DENIED``/``CANCELLED``: nothing to book.
    """

    request: RefundRequestInput
    outcome: RefundOutcome


type LedgerInput = RecipeInput | RefundFinished
"""What :func:`apply_recipes` accepts: the domain recipe inputs plus application-level inputs."""


@dataclass(frozen=True, slots=True)
class RecipeResult:
    """Stored entries touched by the recipes, in input order: created, replayed or transitioned.

    A recipe whose every leg is zero (e.g. R2a with no fee change) contributes nothing.
    """

    entries: tuple[JournalEntry, ...]


async def apply_recipe(
    uow: UnitOfWork,
    recipe_input: LedgerInput,
    *,
    context: RecipeContext,
    aggregate_lock: AggregateLock | None = None,
) -> RecipeResult:
    """Apply one recipe in the caller's transaction (no commit). See :func:`apply_recipes`."""
    return await apply_recipes(uow, [recipe_input], context=context, aggregate_lock=aggregate_lock)


async def apply_recipes(
    uow: UnitOfWork,
    recipe_inputs: Sequence[LedgerInput],
    *,
    context: RecipeContext,
    aggregate_lock: AggregateLock | None = None,
) -> RecipeResult:
    """Apply recipes that belong to one unit of work (R1+R7, R2+R3, ...) as a single ledger write.

    The ledger allows one locking write per unit of work, so recipes of the same transaction must
    be applied together. Idempotent: replaying the same facts returns the stored entries.
    """
    if any(isinstance(item, ReceivedInCash) for item in recipe_inputs):
        raise ReceivedInCashRejected(
            "RECEIVED_IN_CASH is not supported: money outside the provider is never booked"
        )
    if aggregate_lock is not None:
        await aggregate_lock()
    ledger = LedgerService(uow)
    items: list[WriteItem] = []
    for recipe_input in recipe_inputs:
        items.extend(await _plan(ledger, context, recipe_input))
    if not items:
        return RecipeResult(())
    written = await ledger.write(context.project_id, items)
    return RecipeResult(tuple(entry for entry in written if entry is not None))


async def booked_capture(uow: UnitOfWork, project_id: UUID, payment_id: UUID) -> RecipeCode | None:
    """How the capture of ``payment_id`` was booked: ``R2`` (or ``R6``), ``R2b``, or not at all.

    Later recipes of the payment depend on it (an R2b payment is refunded from unapplied cash).
    """
    ledger = LedgerService(uow)
    for key in (payment_captured_key(payment_id), _unapplied_key(payment_id)):
        entry = await ledger.find_entry_by_key(project_id, key)
        if entry is not None:
            return RecipeCode(entry.recipe)
    return None


async def fee_adjustment_booked(
    uow: UnitOfWork, project_id: UUID, payment_id: UUID, net_value: Money
) -> bool:
    """Whether an R2a to ``net_value`` exists already (key ``payment:{id}:fee:{net}``).

    A net value that comes back to an earlier value cannot be booked again under that key.
    """
    key = _fee_key(payment_id, net_value)
    return await LedgerService(uow).find_entry_by_key(project_id, key) is not None


def recipe_key(recipe_input: LedgerInput) -> str | None:
    """Idempotency key of the entry a fact books (``None``: transitions or no single entry).

    Only the facts that book one entry under a key derived from their own ids are mapped.
    """
    match recipe_input:
        case InvoiceFinalized():
            return invoice_finalized_key(recipe_input.invoice_id)
        case PaymentCaptured() | TopUpCaptured():
            return payment_captured_key(recipe_input.payment_id)
        case FeeAdjusted():
            return _fee_key(recipe_input.payment_id, recipe_input.net_value)
        case UnappliedCashReceived():
            return _unapplied_key(recipe_input.payment_id)
        case SplitAllocated():
            return f"payment:{recipe_input.payment_id}:split"  # build_r3's key
        case PaymentSettled():
            return f"payment:{recipe_input.payment_id}:settled"  # build_r4's key
        case (
            RefundRequested()
            | ProviderSplitRefundRequested()
            | TopUpRefundRequested()
            | UnappliedCashRefundRequested()
        ):
            return refund_pending_key(recipe_input.refund_id)
        case _:
            return None


async def unbooked(
    uow: UnitOfWork, project_id: UUID, recipe_inputs: Sequence[LedgerInput]
) -> list[LedgerInput]:
    """``recipe_inputs`` without the facts whose entry is already booked (see :func:`recipe_key`).

    For callers that derive facts from state transitions: a fact seen again (redelivery, re-run)
    is skipped whatever its ``effective_at``, instead of failing as a reused key.
    """
    ledger = LedgerService(uow)
    fresh: list[LedgerInput] = []
    for recipe_input in recipe_inputs:
        key = recipe_key(recipe_input)
        if key is None or await ledger.find_entry_by_key(project_id, key) is None:
            fresh.append(recipe_input)
    return fresh


def _fee_key(payment_id: UUID, net_value: Money) -> str:
    return f"payment:{payment_id}:fee:{net_value.minor_units}"  # build_r2a's key


def _unapplied_key(payment_id: UUID) -> str:
    return f"payment:{payment_id}:unapplied"  # build_r2b's key


# --- planning -------------------------------------------------------------------------------------


async def _plan(ledger: LedgerService, ctx: RecipeContext, r: LedgerInput) -> Sequence[WriteItem]:
    match r:
        case RefundFinished():
            pending = await ledger.find_entry_by_key(
                ctx.project_id, refund_pending_key(r.request.refund_id)
            )
            if pending is not None:
                done = r.outcome is RefundOutcome.DONE
                return [
                    Transition(pending.id, EntryStatus.POSTED if done else EntryStatus.ARCHIVED)
                ]
            if r.outcome is not RefundOutcome.DONE:
                return []
            return [_as_posted(item) for item in await _plan(ledger, ctx, r.request)]
        case InvoiceFinalized():
            return build_r1(ctx, r)
        case WalletConsumed():
            return build_r7(ctx, r)
        case PaymentCaptured():
            return build_r2(ctx, r)
        case FeeAdjusted():
            return build_r2a(ctx, r)
        case UnappliedCashReceived():
            return build_r2b(ctx, r)
        case SplitAllocated():
            return build_r3(ctx, r)
        case PaymentSettled():
            return build_r4(ctx, r)
        case ProviderSplitDone():
            return build_r5_done(ctx, r)
        case ProviderSplitBlocked():
            return build_r5_blocked(ctx, r)
        case ProviderSplitUnblocked():
            blocked = await _required(ledger, ctx, split_blocked_key(r.split_id))
            return [
                await _reversal(
                    ledger,
                    ctx,
                    blocked,
                    f"split:{r.split_id}:unblocked",
                    RecipeCode.R5,
                    {"split_id": str(r.split_id)},
                )
            ]
        case TopUpCaptured():
            return build_r6(ctx, r)
        case RefundRequested():
            currency = r.amount.currency
            return [
                _deferred(
                    refund_pending_key(r.refund_id),
                    [*balance_accounts(r), PLATFORM_REVENUE.spec(), r.source],
                    currency,
                    lambda balances: build_r8_internal(ctx, r, balances),
                )
            ]
        case ProviderSplitRefundRequested():
            return build_r8_provider_split(ctx, r)
        case TopUpRefundRequested():
            return [
                _deferred(
                    refund_pending_key(r.refund_id),
                    [*balance_accounts(r), r.source],
                    r.amount.currency,
                    lambda balances: build_r8_top_up(ctx, r, balances),
                )
            ]
        case UnappliedCashRefundRequested():
            return build_r8_unapplied(ctx, r)
        case RefundCompleted():
            pending = await _required(ledger, ctx, refund_pending_key(r.refund_id))
            done = r.outcome is RefundOutcome.DONE
            return [Transition(pending.id, EntryStatus.POSTED if done else EntryStatus.ARCHIVED)]
        case DisputeOpened():
            provider = r.provider_account_id
            return [
                _deferred(
                    dispute_opened_key(r.dispute_id),
                    [
                        *balance_accounts(r),
                        PROVIDER_BALANCE_HELD.spec(provider),
                        PROVIDER_BALANCE.spec(provider),
                    ],
                    r.amount.currency,
                    lambda balances: build_r9_opened(ctx, r, balances),
                )
            ]
        case DisputeWon():
            opened = await _required(ledger, ctx, dispute_opened_key(r.dispute_id))
            await _not_resolved(ledger, ctx, f"dispute:{r.dispute_id}:lost")
            return [
                await _reversal(
                    ledger,
                    ctx,
                    opened,
                    f"dispute:{r.dispute_id}:won",
                    RecipeCode.R9,
                    {"dispute_id": str(r.dispute_id)},
                )
            ]
        case DisputeLost():
            opened = await _required(ledger, ctx, dispute_opened_key(r.dispute_id))
            await _not_resolved(ledger, ctx, f"dispute:{r.dispute_id}:won")
            return build_r9_lost(ctx, r, await _frozen(ledger, ctx, opened))
        case PayoutRequested():
            return build_r10_request(ctx, r)
        case PayoutPaid():
            pending = await _required(ledger, ctx, payout_pending_key(r.payout_id))
            return [Transition(pending.id, EntryStatus.POSTED), *build_r10_fee(ctx, r)]
        case PayoutFailed():
            pending = await _required(ledger, ctx, payout_pending_key(r.payout_id))
            return [Transition(pending.id, EntryStatus.ARCHIVED)]
        case PayoutReturned():
            paid = await _required(ledger, ctx, payout_pending_key(r.payout_id))
            return [
                await _reversal(
                    ledger,
                    ctx,
                    paid,
                    f"payout:{r.payout_id}:returned",
                    RecipeCode.R10,
                    {"payout_id": str(r.payout_id)},
                )
            ]
        case InvoiceVoided():
            planned: list[WriteItem] = []
            for part, key in (
                ("finalized", invoice_finalized_key(r.invoice_id)),
                ("wallet", invoice_wallet_key(r.invoice_id)),
            ):
                entry = await ledger.find_entry_by_key(ctx.project_id, key)
                if entry is not None:
                    planned.append(
                        await _reversal(
                            ledger,
                            ctx,
                            entry,
                            f"invoice:{r.invoice_id}:void:{part}",
                            RecipeCode.R11,
                            {"invoice_id": str(r.invoice_id)},
                        )
                    )
            return planned
        case ReconciliationAdjustment():
            return build_r12(ctx, r)
        case WriteOff():
            return build_r13(ctx, r)
        case ReceivedInCash():  # pragma: no cover - rejected before planning
            raise ReceivedInCashRejected("RECEIVED_IN_CASH is not supported")
        case _:  # pragma: no cover - exhaustive
            assert_never(r)


def _as_posted(item: WriteItem) -> WriteItem:
    """A step-1 refund item booked directly as posted (same key, same legs)."""
    if isinstance(item, EntryDraft):
        return replace(item, status=EntryStatus.POSTED)
    if isinstance(item, DeferredEntry):
        build = item.build

        def posted(locked: Mapping[tuple[str, str], Account]) -> EntryDraft | None:
            draft = build(locked)
            return None if draft is None else replace(draft, status=EntryStatus.POSTED)

        return DeferredEntry(item.idempotency_key, item.accounts, posted)
    raise TypeError("a refund request plans entries, not transitions")  # pragma: no cover


def _deferred(
    key: str,
    accounts: Sequence[AccountSpec],
    currency: Currency,
    build: Callable[[Mapping[str, Balance]], tuple[EntryDraft, ...]],
) -> DeferredEntry:
    def from_locked(locked: Mapping[tuple[str, str], Account]) -> EntryDraft | None:
        balances = {
            code: account.balance
            for (code, code_currency), account in locked.items()
            if code_currency == currency.code
        }
        built = build(balances)
        return built[0] if built else None

    return DeferredEntry(key, tuple((spec, currency) for spec in accounts), from_locked)


async def _required(ledger: LedgerService, ctx: RecipeContext, key: str) -> JournalEntry:
    entry = await ledger.find_entry_by_key(ctx.project_id, key)
    if entry is None:
        raise NotFound(f"no ledger entry {key!r} to continue from")
    return entry


async def _not_resolved(ledger: LedgerService, ctx: RecipeContext, key: str) -> None:
    if await ledger.find_entry_by_key(ctx.project_id, key) is not None:
        raise Conflict(f"dispute already resolved ({key!r})")


async def _reversal(
    ledger: LedgerService,
    ctx: RecipeContext,
    entry: JournalEntry,
    key: str,
    recipe: RecipeCode,
    metadata: Mapping[str, JsonValue],
) -> EntryDraft:
    return await ledger.build_reversal(
        ctx.project_id,
        entry.id,
        key,
        effective_at=ctx.effective_at,
        recipe=recipe.value,
        correlation_id=ctx.correlation_id,
        causation_id=ctx.causation_id,
        metadata=metadata,
    )


async def _frozen(
    ledger: LedgerService, ctx: RecipeContext, opened: JournalEntry
) -> dict[PayeeId, Money]:
    """Per payee, what the R9 opened entry credited to ``payee_payable_held``."""
    accounts = await ledger.get_accounts(ctx.project_id, (p.account_id for p in opened.postings))
    frozen: dict[PayeeId, Money] = {}
    for posting in opened.postings:
        spec = accounts[posting.account_id].spec
        held = template_of(spec.code) is PAYEE_PAYABLE_HELD and spec.payee_id is not None
        if posting.direction is Direction.CREDIT and held:
            payee = PayeeId(cast(UUID, spec.payee_id))
            frozen[payee] = frozen.get(payee, Money.zero(posting.amount.currency)) + posting.amount
    return frozen
