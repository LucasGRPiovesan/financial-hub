"""Payments persistence on the caller's session (runs as ``fh_app``: no DELETE, column UPDATE).

Every product-facing read is scoped by ``project_id``. Outcome writes are compare-and-set on the
fencing token (``attempt_id``) plus the expected status (ADR-0012 s3): they return whether *this*
writer won, and a loser never overwrites what the winner recorded.
"""

from collections.abc import Collection, Mapping, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from financial_hub.modules.payments.domain import (
    Attempt,
    AttemptStatus,
    CaptureStatus,
    CreationStatus,
    DisputeStatus,
    PaymentIntent,
    PaymentMethod,
    PaymentState,
    ProviderPayment,
    Refund,
    RefundState,
    SettlementStatus,
)
from financial_hub.modules.payments.infrastructure.tables import (
    payment_attempts,
    payment_intents,
    provider_payments,
    refunds,
)
from financial_hub.shared.money import Currency, Money
from financial_hub.shared.pagination import Cursor, keyset_predicate

_intents = payment_intents
_payments = provider_payments


def _intent(row: Any) -> PaymentIntent:
    return PaymentIntent(
        id=row.id,
        project_id=row.project_id,
        customer_id=row.customer_id,
        amount=Money(int(row.amount), Currency.of(row.currency)),
        method=PaymentMethod(row.method),
        due_date=row.due_date,
        external_ref=row.external_ref,
        description=row.description,
        metadata=dict(row.metadata),
        provider=row.provider,
        provider_account_id=row.provider_account_id,
        creation_status=CreationStatus(row.creation_status),
        attempt_id=row.attempt_id,
        failure_code=row.failure_code,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _provider_payment(row: Any) -> ProviderPayment:
    return ProviderPayment(
        id=row.id,
        payment_intent_id=row.payment_intent_id,
        project_id=row.project_id,
        provider=row.provider,
        provider_account_id=row.provider_account_id,
        provider_payment_id=row.provider_payment_id,
        raw_status=row.raw_status,
        state=PaymentState(
            capture_status=CaptureStatus(row.capture_status),
            settlement_status=SettlementStatus(row.settlement_status),
            dispute_status=DisputeStatus(row.dispute_status),
            captured_amount=int(row.captured_amount),
            net_amount=None if row.net_amount is None else int(row.net_amount),
            fee_estimated=None if row.fee_estimated is None else int(row.fee_estimated),
        ),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _refund(row: Any) -> Refund:
    return Refund(
        id=row.id,
        project_id=row.project_id,
        payment_intent_id=row.payment_intent_id,
        provider_payment_id=row.provider_payment_id,
        amount=Money(int(row.amount), Currency.of(row.currency)),
        status=RefundState(row.status),
        reason=row.reason,
        provider_refund_id=row.provider_refund_id,
        attempt_id=row.attempt_id,
        origin=row.origin,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


# --- payment intents ----------------------------------------------------------------------------


async def insert_intent(session: AsyncSession, intent: PaymentIntent) -> bool:
    """``False`` when the project already has an intent with this ``external_ref``."""
    statement = (
        insert(_intents)
        .values(
            id=intent.id,
            project_id=intent.project_id,
            customer_id=intent.customer_id,
            amount=intent.amount.minor_units,
            currency=intent.amount.currency.code,
            method=intent.method.value,
            due_date=intent.due_date,
            external_ref=intent.external_ref,
            description=intent.description,
            metadata=dict(intent.metadata),
            provider=intent.provider,
            provider_account_id=intent.provider_account_id,
            creation_status=intent.creation_status.value,
            attempt_id=intent.attempt_id,
            failure_code=intent.failure_code,
            created_at=intent.created_at,
            updated_at=intent.updated_at,
        )
        .on_conflict_do_nothing(constraint="uq_payment_intents_project_id_external_ref")
        .returning(_intents.c.id)
    )
    return (await session.execute(statement)).first() is not None


async def get_intent(
    session: AsyncSession, project_id: UUID, intent_id: UUID, *, for_update: bool = False
) -> PaymentIntent | None:
    statement = select(_intents).where(
        _intents.c.project_id == project_id, _intents.c.id == intent_id
    )
    if for_update:
        statement = statement.with_for_update()
    row = (await session.execute(statement)).first()
    return None if row is None else _intent(row)


async def find_intent(session: AsyncSession, intent_id: UUID) -> PaymentIntent | None:
    """Any project's intent: for the resolver and internal follow-ups, never an API response."""
    row = (await session.execute(select(_intents).where(_intents.c.id == intent_id))).first()
    return None if row is None else _intent(row)


async def list_intents(
    session: AsyncSession, project_id: UUID, *, cursor: Cursor | None, limit: int
) -> Sequence[PaymentIntent]:
    """Newest first, ``limit`` rows at most (callers pass ``limit + 1`` to detect more)."""
    statement = select(_intents).where(_intents.c.project_id == project_id)
    if cursor is not None:
        statement = statement.where(keyset_predicate(_intents.c.created_at, _intents.c.id, cursor))
    statement = statement.order_by(_intents.c.created_at.desc(), _intents.c.id.desc()).limit(limit)
    return [_intent(row) for row in (await session.execute(statement)).all()]


async def record_creation(
    session: AsyncSession,
    *,
    intent_id: UUID,
    attempt_id: UUID,
    expected: Collection[CreationStatus],
    status: CreationStatus,
    failure_code: str | None,
    now: datetime,
) -> bool:
    """Fenced write of the creation outcome: only the holder of the current token may record it."""
    statement = (
        update(_intents)
        .where(
            _intents.c.id == intent_id,
            _intents.c.attempt_id == attempt_id,
            _intents.c.creation_status.in_([value.value for value in expected]),
        )
        .values(creation_status=status.value, failure_code=failure_code, updated_at=now)
        .returning(_intents.c.id)
    )
    return (await session.execute(statement)).first() is not None


async def replace_creation_attempt(
    session: AsyncSession,
    *,
    intent_id: UUID,
    old_attempt_id: UUID,
    new_attempt_id: UUID,
    now: datetime,
) -> bool:
    """Move the fencing token to a new attempt (after a confirmed absence); a stale holder loses."""
    statement = (
        update(_intents)
        .where(
            _intents.c.id == intent_id,
            _intents.c.attempt_id == old_attempt_id,
            _intents.c.creation_status.in_(
                [CreationStatus.CREATE_REQUESTED.value, CreationStatus.UNKNOWN.value]
            ),
        )
        .values(
            attempt_id=new_attempt_id,
            creation_status=CreationStatus.CREATE_REQUESTED.value,
            updated_at=now,
        )
        .returning(_intents.c.id)
    )
    return (await session.execute(statement)).first() is not None


async def unresolved_intents(
    session: AsyncSession, *, requested_before: datetime, limit: int
) -> Sequence[PaymentIntent]:
    """Intents whose creation outcome is unknown, or requested and untouched since the cutoff.

    Cross-project on purpose: this feeds the resolver worker, never an API response.
    """
    unknown = _intents.c.creation_status == CreationStatus.UNKNOWN.value
    stuck = (_intents.c.creation_status == CreationStatus.CREATE_REQUESTED.value) & (
        _intents.c.updated_at < requested_before
    )
    statement = (
        select(_intents)
        .where(unknown | stuck)
        .order_by(_intents.c.updated_at, _intents.c.id)
        .limit(limit)
    )
    return [_intent(row) for row in (await session.execute(statement)).all()]


# --- provider payments --------------------------------------------------------------------------


async def insert_provider_payment(session: AsyncSession, payment: ProviderPayment) -> None:
    state = payment.state
    await session.execute(
        insert(_payments).values(
            id=payment.id,
            payment_intent_id=payment.payment_intent_id,
            project_id=payment.project_id,
            provider=payment.provider,
            provider_account_id=payment.provider_account_id,
            provider_payment_id=payment.provider_payment_id,
            raw_status=payment.raw_status,
            capture_status=state.capture_status.value,
            settlement_status=state.settlement_status.value,
            dispute_status=state.dispute_status.value,
            captured_amount=state.captured_amount,
            net_amount=state.net_amount,
            fee_estimated=state.fee_estimated,
            created_at=payment.created_at,
            updated_at=payment.updated_at,
        )
    )


async def get_provider_payment(
    session: AsyncSession, intent_id: UUID, *, for_update: bool = False
) -> ProviderPayment | None:
    """The intent's provider payment (at most one, ``uq_provider_payments_payment_intent_id``)."""
    statement = select(_payments).where(_payments.c.payment_intent_id == intent_id)
    if for_update:
        statement = statement.with_for_update()
    row = (await session.execute(statement)).first()
    return None if row is None else _provider_payment(row)


async def find_intent_by_provider_payment(
    session: AsyncSession, *, provider: str, provider_account_id: UUID, provider_payment_id: str
) -> PaymentIntent | None:
    """The intent behind a provider payment id on one account (routing an inbound webhook)."""
    statement = (
        select(_intents)
        .join(_payments, _payments.c.payment_intent_id == _intents.c.id)
        .where(
            _payments.c.provider == provider,
            _payments.c.provider_account_id == provider_account_id,
            _payments.c.provider_payment_id == provider_payment_id,
        )
    )
    row = (await session.execute(statement)).first()
    return None if row is None else _intent(row)


async def provider_payments_for(
    session: AsyncSession, intent_ids: Collection[UUID]
) -> Mapping[UUID, ProviderPayment]:
    if not intent_ids:
        return {}
    statement = select(_payments).where(_payments.c.payment_intent_id.in_(list(intent_ids)))
    rows = (await session.execute(statement)).all()
    return {row.payment_intent_id: _provider_payment(row) for row in rows}


async def update_provider_payment(
    session: AsyncSession, payment_id: UUID, *, state: PaymentState, raw_status: str, now: datetime
) -> None:
    await session.execute(
        update(_payments)
        .where(_payments.c.id == payment_id)
        .values(
            raw_status=raw_status,
            capture_status=state.capture_status.value,
            settlement_status=state.settlement_status.value,
            dispute_status=state.dispute_status.value,
            captured_amount=state.captured_amount,
            net_amount=state.net_amount,
            fee_estimated=state.fee_estimated,
            updated_at=now,
        )
    )


# --- refunds ------------------------------------------------------------------------------------


async def insert_refund(session: AsyncSession, refund: Refund) -> None:
    await session.execute(
        insert(refunds).values(
            id=refund.id,
            project_id=refund.project_id,
            payment_intent_id=refund.payment_intent_id,
            provider_payment_id=refund.provider_payment_id,
            amount=refund.amount.minor_units,
            currency=refund.amount.currency.code,
            status=refund.status.value,
            reason=refund.reason,
            provider_refund_id=refund.provider_refund_id,
            attempt_id=refund.attempt_id,
            origin=refund.origin,
            created_at=refund.created_at,
            updated_at=refund.updated_at,
        )
    )


async def get_refund(session: AsyncSession, project_id: UUID, refund_id: UUID) -> Refund | None:
    statement = select(refunds).where(refunds.c.project_id == project_id, refunds.c.id == refund_id)
    row = (await session.execute(statement)).first()
    return None if row is None else _refund(row)


async def find_refund(session: AsyncSession, refund_id: UUID) -> Refund | None:
    """Any project's refund: for the resolver, never an API response."""
    row = (await session.execute(select(refunds).where(refunds.c.id == refund_id))).first()
    return None if row is None else _refund(row)


async def refunds_of(session: AsyncSession, intent_ids: Collection[UUID]) -> Sequence[Refund]:
    """Refunds of the given intents, oldest first."""
    if not intent_ids:
        return []
    statement = (
        select(refunds)
        .where(refunds.c.payment_intent_id.in_(list(intent_ids)))
        .order_by(refunds.c.created_at, refunds.c.id)
    )
    return [_refund(row) for row in (await session.execute(statement)).all()]


async def record_refund(
    session: AsyncSession,
    *,
    refund_id: UUID,
    attempt_id: UUID,
    expected: Collection[RefundState],
    status: RefundState,
    provider_refund_id: str | None,
    now: datetime,
) -> bool:
    """Fenced write of a refund outcome (same rules as :func:`record_creation`)."""
    values: dict[str, Any] = {"status": status.value, "updated_at": now}
    if provider_refund_id is not None:
        values["provider_refund_id"] = provider_refund_id
    statement = (
        update(refunds)
        .where(
            refunds.c.id == refund_id,
            refunds.c.attempt_id == attempt_id,
            refunds.c.status.in_([value.value for value in expected]),
        )
        .values(**values)
        .returning(refunds.c.id)
    )
    return (await session.execute(statement)).first() is not None


async def replace_refund_attempt(
    session: AsyncSession,
    *,
    refund_id: UUID,
    old_attempt_id: UUID,
    new_attempt_id: UUID,
    now: datetime,
) -> bool:
    statement = (
        update(refunds)
        .where(
            refunds.c.id == refund_id,
            refunds.c.attempt_id == old_attempt_id,
            refunds.c.status.in_([RefundState.REQUESTED.value, RefundState.UNKNOWN.value]),
        )
        .values(attempt_id=new_attempt_id, status=RefundState.REQUESTED.value, updated_at=now)
        .returning(refunds.c.id)
    )
    return (await session.execute(statement)).first() is not None


async def unresolved_refunds(
    session: AsyncSession, *, requested_before: datetime, limit: int
) -> Sequence[Refund]:
    """Hub refunds whose outcome is unknown, or requested and untouched since the cutoff."""
    unknown = refunds.c.status == RefundState.UNKNOWN.value
    stuck = (refunds.c.status == RefundState.REQUESTED.value) & (
        refunds.c.updated_at < requested_before
    )
    statement = (
        select(refunds)
        .where(refunds.c.origin == "hub", unknown | stuck)
        .order_by(refunds.c.updated_at, refunds.c.id)
        .limit(limit)
    )
    return [_refund(row) for row in (await session.execute(statement)).all()]


# --- attempts -----------------------------------------------------------------------------------


async def insert_attempt(session: AsyncSession, attempt: Attempt) -> None:
    await session.execute(
        insert(payment_attempts).values(
            id=attempt.id,
            project_id=attempt.project_id,
            payment_intent_id=attempt.payment_intent_id,
            refund_id=attempt.refund_id,
            kind=attempt.kind.value,
            status=attempt.status.value,
            request_hash=attempt.request_hash,
            outcome=None,
            created_at=attempt.created_at,
            completed_at=None,
        )
    )


async def finish_attempt(
    session: AsyncSession,
    attempt_id: UUID,
    *,
    status: AttemptStatus,
    outcome: Mapping[str, Any],
    now: datetime,
) -> bool:
    """Close an open (``requested``/``unknown``) attempt; a closed one is never rewritten."""
    statement = (
        update(payment_attempts)
        .where(
            payment_attempts.c.id == attempt_id,
            payment_attempts.c.status.in_(
                [AttemptStatus.REQUESTED.value, AttemptStatus.UNKNOWN.value]
            ),
        )
        .values(status=status.value, outcome=dict(outcome), completed_at=now)
        .returning(payment_attempts.c.id)
    )
    return (await session.execute(statement)).first() is not None


async def mark_attempt_unknown(
    session: AsyncSession, attempt_id: UUID, *, outcome: Mapping[str, Any]
) -> bool:
    statement = (
        update(payment_attempts)
        .where(
            payment_attempts.c.id == attempt_id,
            payment_attempts.c.status == AttemptStatus.REQUESTED.value,
        )
        .values(status=AttemptStatus.UNKNOWN.value, outcome=dict(outcome))
        .returning(payment_attempts.c.id)
    )
    return (await session.execute(statement)).first() is not None
