"""Plans, subscriptions and invoices on the caller's session (runs as ``fh_app``).

``plans``, ``plan_prices`` and ``invoice_lines`` are append-only; subscriptions and invoices only
move their status columns (column grants plus the ``*_guard`` triggers of ``0013_billing``).
Every read is scoped by ``project_id`` except the period-close and payment lookups the worker
runs across projects, which return ids the caller then reads with their project.
"""

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from financial_hub.modules.billing.domain import (
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    LineKind,
    Plan,
    Subscription,
    SubscriptionStatus,
)
from financial_hub.modules.billing.infrastructure.tables import (
    invoice_lines,
    invoices,
    plan_prices,
    plans,
    subscriptions,
)
from financial_hub.shared.pagination import Cursor, keyset_predicate

# --- plans --------------------------------------------------------------------------------------


async def insert_plan(session: AsyncSession, plan: Plan) -> bool:
    """Insert the plan and its prices; ``False`` when the project already has the code."""
    statement = (
        insert(plans)
        .values(
            id=plan.id,
            project_id=plan.project_id,
            code=plan.code,
            name=plan.name,
            currency=plan.currency,
            billing_interval=plan.billing_interval,
            created_at=plan.created_at,
        )
        .on_conflict_do_nothing(index_elements=[plans.c.project_id, plans.c.code])
        .returning(plans.c.id)
    )
    if (await session.execute(statement)).first() is None:
        return False
    await session.execute(
        insert(plan_prices),
        [
            {
                "plan_id": plan.id,
                "price_id": price_id,
                "project_id": plan.project_id,
                "position": position,
            }
            for position, price_id in enumerate(plan.price_ids)
        ],
    )
    return True


async def _plans(session: AsyncSession, rows: Sequence[Any]) -> list[Plan]:
    if not rows:
        return []
    statement = (
        select(plan_prices.c.plan_id, plan_prices.c.price_id)
        .where(plan_prices.c.plan_id.in_([row.id for row in rows]))
        .order_by(plan_prices.c.plan_id, plan_prices.c.position)
    )
    prices: dict[UUID, list[UUID]] = defaultdict(list)
    for row in (await session.execute(statement)).all():
        prices[row.plan_id].append(row.price_id)
    return [
        Plan(
            id=row.id,
            project_id=row.project_id,
            code=row.code,
            name=row.name,
            currency=row.currency,
            price_ids=tuple(prices[row.id]),
            created_at=row.created_at,
            billing_interval=row.billing_interval,
        )
        for row in rows
    ]


async def get_plan(session: AsyncSession, project_id: UUID, code: str) -> Plan | None:
    statement = select(plans).where(plans.c.project_id == project_id, plans.c.code == code)
    found = await _plans(session, (await session.execute(statement)).all())
    return found[0] if found else None


async def get_plans(
    session: AsyncSession, project_id: UUID, plan_ids: Sequence[UUID]
) -> list[Plan]:
    if not plan_ids:
        return []
    statement = select(plans).where(plans.c.project_id == project_id, plans.c.id.in_(plan_ids))
    return await _plans(session, (await session.execute(statement)).all())


async def list_plans(
    session: AsyncSession, project_id: UUID, *, cursor: Cursor | None, limit: int
) -> list[Plan]:
    statement = select(plans).where(plans.c.project_id == project_id)
    if cursor is not None:
        statement = statement.where(keyset_predicate(plans.c.created_at, plans.c.id, cursor))
    statement = statement.order_by(plans.c.created_at.desc(), plans.c.id.desc()).limit(limit)
    return await _plans(session, (await session.execute(statement)).all())


# --- subscriptions ------------------------------------------------------------------------------


def _subscription(row: Any) -> Subscription:
    return Subscription(
        id=row.id,
        project_id=row.project_id,
        customer_id=row.customer_id,
        plan_id=row.plan_id,
        status=SubscriptionStatus(row.status),
        anchor_day=row.anchor_day,
        started_at=row.started_at,
        trial_end=row.trial_end,
        current_period_start=row.current_period_start,
        current_period_end=row.current_period_end,
        cancel_at_period_end=row.cancel_at_period_end,
        canceled_at=row.canceled_at,
        ended_at=row.ended_at,
        payment_method=row.payment_method,
        days_until_due=row.days_until_due,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def insert_subscription(session: AsyncSession, subscription: Subscription) -> None:
    """Raises ``IntegrityError`` (``uq_subscriptions_live``) for a second live subscription."""
    await session.execute(
        insert(subscriptions).values(
            id=subscription.id,
            project_id=subscription.project_id,
            customer_id=subscription.customer_id,
            plan_id=subscription.plan_id,
            status=subscription.status.value,
            anchor_day=subscription.anchor_day,
            started_at=subscription.started_at,
            trial_end=subscription.trial_end,
            current_period_start=subscription.current_period_start,
            current_period_end=subscription.current_period_end,
            cancel_at_period_end=subscription.cancel_at_period_end,
            canceled_at=subscription.canceled_at,
            ended_at=subscription.ended_at,
            payment_method=subscription.payment_method,
            days_until_due=subscription.days_until_due,
            created_at=subscription.created_at,
            updated_at=subscription.updated_at,
        )
    )


async def update_subscription(session: AsyncSession, subscription: Subscription) -> None:
    """Write the mutable columns (status, current period, cancellation)."""
    await session.execute(
        update(subscriptions)
        .where(
            subscriptions.c.id == subscription.id,
            subscriptions.c.project_id == subscription.project_id,
        )
        .values(
            status=subscription.status.value,
            current_period_start=subscription.current_period_start,
            current_period_end=subscription.current_period_end,
            cancel_at_period_end=subscription.cancel_at_period_end,
            canceled_at=subscription.canceled_at,
            ended_at=subscription.ended_at,
            updated_at=subscription.updated_at,
        )
    )


async def get_subscription(
    session: AsyncSession, project_id: UUID, subscription_id: UUID, *, lock: bool = False
) -> Subscription | None:
    statement = select(subscriptions).where(
        subscriptions.c.id == subscription_id, subscriptions.c.project_id == project_id
    )
    if lock:
        statement = statement.with_for_update()
    row = (await session.execute(statement)).first()
    return None if row is None else _subscription(row)


async def find_live_subscription(
    session: AsyncSession, project_id: UUID, customer_id: UUID, plan_id: UUID
) -> Subscription | None:
    statement = select(subscriptions).where(
        subscriptions.c.project_id == project_id,
        subscriptions.c.customer_id == customer_id,
        subscriptions.c.plan_id == plan_id,
        subscriptions.c.status != SubscriptionStatus.CANCELED.value,
    )
    row = (await session.execute(statement)).first()
    return None if row is None else _subscription(row)


async def list_subscriptions(
    session: AsyncSession,
    project_id: UUID,
    *,
    cursor: Cursor | None,
    limit: int,
    customer_id: UUID | None = None,
) -> list[Subscription]:
    statement = select(subscriptions).where(subscriptions.c.project_id == project_id)
    if customer_id is not None:
        statement = statement.where(subscriptions.c.customer_id == customer_id)
    if cursor is not None:
        statement = statement.where(
            keyset_predicate(subscriptions.c.created_at, subscriptions.c.id, cursor)
        )
    statement = statement.order_by(
        subscriptions.c.created_at.desc(), subscriptions.c.id.desc()
    ).limit(limit)
    return [_subscription(row) for row in (await session.execute(statement)).all()]


async def due_subscriptions(
    session: AsyncSession, now: datetime, *, limit: int
) -> list[tuple[UUID, UUID]]:
    """``(project_id, id)`` of live subscriptions whose current period ended (all projects)."""
    statement = (
        select(subscriptions.c.project_id, subscriptions.c.id)
        .where(
            subscriptions.c.status != SubscriptionStatus.CANCELED.value,
            subscriptions.c.current_period_end <= now,
        )
        .order_by(subscriptions.c.current_period_end, subscriptions.c.id)
        .limit(limit)
    )
    return [(row.project_id, row.id) for row in (await session.execute(statement)).all()]


# --- invoices -----------------------------------------------------------------------------------


def _line(row: Any) -> InvoiceLine:
    return InvoiceLine(
        id=row.id,
        position=row.position,
        kind=LineKind(row.kind),
        price_id=row.price_id,
        price_code=row.price_code,
        price_version_id=row.price_version_id,
        price_version=row.price_version,
        model=row.model,
        metric_code=row.metric_code,
        quantity=row.quantity,
        amount=row.amount,
        rounding=row.rounding,
        details=row.details,
    )


async def _invoices(session: AsyncSession, rows: Sequence[Any]) -> list[Invoice]:
    if not rows:
        return []
    statement = (
        select(invoice_lines)
        .where(invoice_lines.c.invoice_id.in_([row.id for row in rows]))
        .order_by(invoice_lines.c.invoice_id, invoice_lines.c.position)
    )
    lines: dict[UUID, list[InvoiceLine]] = defaultdict(list)
    for row in (await session.execute(statement)).all():
        lines[row.invoice_id].append(_line(row))
    return [
        Invoice(
            id=row.id,
            project_id=row.project_id,
            subscription_id=row.subscription_id,
            customer_id=row.customer_id,
            currency=row.currency,
            status=InvoiceStatus(row.status),
            period_start=row.period_start,
            period_end=row.period_end,
            total=row.total,
            credits_applied=row.credits_applied,
            amount_due=row.amount_due,
            payment_intent_id=row.payment_intent_id,
            due_date=row.due_date,
            finalized_at=row.finalized_at,
            paid_at=row.paid_at,
            voided_at=row.voided_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
            lines=tuple(lines[row.id]),
        )
        for row in rows
    ]


async def insert_invoice(session: AsyncSession, invoice: Invoice) -> bool:
    """Insert a draft with its lines; ``False`` when the period is already invoiced."""
    statement = (
        insert(invoices)
        .values(
            id=invoice.id,
            project_id=invoice.project_id,
            subscription_id=invoice.subscription_id,
            customer_id=invoice.customer_id,
            currency=invoice.currency,
            status=invoice.status.value,
            period_start=invoice.period_start,
            period_end=invoice.period_end,
            total=invoice.total,
            credits_applied=invoice.credits_applied,
            amount_due=invoice.amount_due,
            created_at=invoice.created_at,
            updated_at=invoice.updated_at,
        )
        .on_conflict_do_nothing(
            index_elements=[invoices.c.subscription_id, invoices.c.period_start]
        )
        .returning(invoices.c.id)
    )
    if (await session.execute(statement)).first() is None:
        return False
    if invoice.lines:
        await session.execute(
            insert(invoice_lines),
            [
                {
                    "id": line.id,
                    "invoice_id": invoice.id,
                    "project_id": invoice.project_id,
                    "position": line.position,
                    "kind": line.kind.value,
                    "price_id": line.price_id,
                    "price_code": line.price_code,
                    "price_version_id": line.price_version_id,
                    "price_version": line.price_version,
                    "model": line.model,
                    "metric_code": line.metric_code,
                    "quantity": line.quantity,
                    "amount": line.amount,
                    "rounding": line.rounding,
                    "details": dict(line.details),
                }
                for line in invoice.lines
            ],
        )
    return True


async def update_invoice(session: AsyncSession, invoice: Invoice) -> None:
    """Write the status columns (the amounts and lines never change)."""
    await session.execute(
        update(invoices)
        .where(invoices.c.id == invoice.id, invoices.c.project_id == invoice.project_id)
        .values(
            status=invoice.status.value,
            payment_intent_id=invoice.payment_intent_id,
            due_date=invoice.due_date,
            finalized_at=invoice.finalized_at,
            paid_at=invoice.paid_at,
            voided_at=invoice.voided_at,
            updated_at=invoice.updated_at,
        )
    )


async def get_invoice(
    session: AsyncSession, project_id: UUID, invoice_id: UUID, *, lock: bool = False
) -> Invoice | None:
    statement = select(invoices).where(
        invoices.c.id == invoice_id, invoices.c.project_id == project_id
    )
    if lock:
        statement = statement.with_for_update()
    found = await _invoices(session, (await session.execute(statement)).all())
    return found[0] if found else None


async def find_invoice_by_intent(
    session: AsyncSession, intent_id: UUID, *, lock: bool = False
) -> Invoice | None:
    """The invoice an intent collects (any project: the intent id is the scope)."""
    statement = select(invoices).where(invoices.c.payment_intent_id == intent_id)
    if lock:
        statement = statement.with_for_update()
    found = await _invoices(session, (await session.execute(statement)).all())
    return found[0] if found else None


async def find_invoice_by_period(
    session: AsyncSession, project_id: UUID, subscription_id: UUID, period_start: datetime
) -> Invoice | None:
    statement = select(invoices).where(
        invoices.c.project_id == project_id,
        invoices.c.subscription_id == subscription_id,
        invoices.c.period_start == period_start,
    )
    found = await _invoices(session, (await session.execute(statement)).all())
    return found[0] if found else None


async def list_invoices(
    session: AsyncSession,
    project_id: UUID,
    *,
    cursor: Cursor | None,
    limit: int,
    subscription_id: UUID | None = None,
) -> list[Invoice]:
    statement = select(invoices).where(invoices.c.project_id == project_id)
    if subscription_id is not None:
        statement = statement.where(invoices.c.subscription_id == subscription_id)
    if cursor is not None:
        statement = statement.where(keyset_predicate(invoices.c.created_at, invoices.c.id, cursor))
    statement = statement.order_by(invoices.c.created_at.desc(), invoices.c.id.desc()).limit(limit)
    return await _invoices(session, (await session.execute(statement)).all())


async def billed_quantity(
    session: AsyncSession, project_id: UUID, subscription_id: UUID, price_id: UUID
) -> Decimal:
    """Usage of ``price_id`` already on the subscription's invoices, void ones included.

    A voided invoice's usage stays counted: voiding forgives it, it is never billed again.
    """
    statement = (
        select(func.coalesce(func.sum(invoice_lines.c.quantity), 0))
        .select_from(
            invoice_lines.join(
                invoices,
                (invoices.c.id == invoice_lines.c.invoice_id)
                & (invoices.c.project_id == invoice_lines.c.project_id),
            )
        )
        .where(
            invoices.c.project_id == project_id,
            invoices.c.subscription_id == subscription_id,
            invoice_lines.c.price_id == price_id,
            invoice_lines.c.kind == LineKind.USAGE.value,
        )
    )
    value = (await session.execute(statement)).scalar_one()
    return Decimal(value)
