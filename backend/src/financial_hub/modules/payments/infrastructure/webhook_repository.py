"""Inbound webhook inbox and customer-provisioning lease (W4.4; runs as ``fh_app``).

Both are written by compare-and-set: an inbox row leaves ``received``/``failed`` once, and a
provisioning outcome is recorded only by the holder of the current ``attempt_id`` (fencing token).
Reads here are cross-project on purpose: they feed the webhook processor and the sweeper, never an
API response.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from financial_hub.modules.payments.domain import (
    PENDING_WEBHOOK_STATES,
    InboundWebhook,
    ProvisionStatus,
    WebhookStatus,
)
from financial_hub.modules.payments.infrastructure.tables import (
    provider_customer_provisions,
    provider_webhook_events,
)

_events = provider_webhook_events
_provisions = provider_customer_provisions
_PENDING = [status.value for status in PENDING_WEBHOOK_STATES]


def _webhook(row: Any) -> InboundWebhook:
    return InboundWebhook(
        id=row.id,
        project_id=row.project_id,
        provider=row.provider,
        provider_account_id=row.provider_account_id,
        provider_event_id=row.provider_event_id,
        event_type=row.event_type,
        body=row.body,
        status=WebhookStatus(row.status),
        outcome=row.outcome,
        attempts=row.attempts,
        last_error=row.last_error,
        received_at=row.received_at,
        processed_at=row.processed_at,
        updated_at=row.updated_at,
    )


# --- inbox --------------------------------------------------------------------------------------


async def insert_webhook(session: AsyncSession, webhook: InboundWebhook) -> bool:
    """Store a notification; ``False`` when this account already holds its provider event id."""
    statement = (
        insert(_events)
        .values(
            id=webhook.id,
            project_id=webhook.project_id,
            provider=webhook.provider,
            provider_account_id=webhook.provider_account_id,
            provider_event_id=webhook.provider_event_id,
            event_type=webhook.event_type,
            body=webhook.body,
            status=webhook.status.value,
            received_at=webhook.received_at,
            updated_at=webhook.updated_at,
        )
        .on_conflict_do_nothing(constraint="uq_provider_webhook_events_provider_event_id")
        .returning(_events.c.id)
    )
    return (await session.execute(statement)).first() is not None


async def get_webhook(session: AsyncSession, webhook_id: UUID) -> InboundWebhook | None:
    row = (await session.execute(select(_events).where(_events.c.id == webhook_id))).first()
    return None if row is None else _webhook(row)


async def finish_webhook(
    session: AsyncSession,
    webhook_id: UUID,
    *,
    status: WebhookStatus,
    outcome: str,
    now: datetime,
) -> bool:
    """Record the final outcome of a pending row; a finished row is never rewritten."""
    statement = (
        update(_events)
        .where(_events.c.id == webhook_id, _events.c.status.in_(_PENDING))
        .values(status=status.value, outcome=outcome[:100], processed_at=now, updated_at=now)
        .returning(_events.c.id)
    )
    return (await session.execute(statement)).first() is not None


async def fail_webhook(
    session: AsyncSession, webhook_id: UUID, *, error: str, now: datetime
) -> None:
    """Count a failed processing attempt; the sweeper retries the row until its limit."""
    await session.execute(
        update(_events)
        .where(_events.c.id == webhook_id, _events.c.status.in_(_PENDING))
        .values(
            status=WebhookStatus.FAILED.value,
            attempts=_events.c.attempts + 1,
            last_error=error[:200],
            updated_at=now,
        )
    )


async def pending_webhooks(
    session: AsyncSession, *, untouched_before: datetime, max_attempts: int, limit: int
) -> Sequence[UUID]:
    """Rows still to process (oldest first), skipping those that used up their attempts."""
    statement = (
        select(_events.c.id)
        .where(
            _events.c.status.in_(_PENDING),
            _events.c.updated_at < untouched_before,
            _events.c.attempts < max_attempts,
        )
        .order_by(_events.c.received_at, _events.c.id)
        .limit(limit)
    )
    return list((await session.execute(statement)).scalars().all())


# --- customer provisioning ----------------------------------------------------------------------


async def claim_provision(
    session: AsyncSession,
    *,
    identity_id: UUID,
    provider_account_id: UUID,
    project_id: UUID,
    attempt_id: UUID,
    lease_until: datetime,
    now: datetime,
) -> bool:
    """Take the provisioning token; ``False`` while another caller's call may be in flight.

    A ``requested`` row keeps its holder until ``lease_until``; an ``unknown`` or ``rejected`` row
    (that call has returned) is taken at once, and a ``created`` one never.
    """
    statement = insert(_provisions).values(
        identity_id=identity_id,
        provider_account_id=provider_account_id,
        project_id=project_id,
        attempt_id=attempt_id,
        status=ProvisionStatus.REQUESTED.value,
        lease_until=lease_until,
        failure_code=None,
        created_at=now,
        updated_at=now,
    )
    current = _provisions.c
    returned = (ProvisionStatus.UNKNOWN.value, ProvisionStatus.REJECTED.value)
    statement = statement.on_conflict_do_update(
        index_elements=[current.identity_id, current.provider_account_id],
        set_={
            "attempt_id": statement.excluded.attempt_id,
            "status": ProvisionStatus.REQUESTED.value,
            "lease_until": statement.excluded.lease_until,
            "failure_code": None,
            "updated_at": statement.excluded.updated_at,
        },
        where=(current.status.in_(returned))
        | ((current.status == ProvisionStatus.REQUESTED.value) & (current.lease_until <= now)),
    ).returning(current.attempt_id)
    row = (await session.execute(statement)).first()
    return row is not None and row.attempt_id == attempt_id


async def finish_provision(
    session: AsyncSession,
    *,
    identity_id: UUID,
    provider_account_id: UUID,
    attempt_id: UUID,
    status: ProvisionStatus,
    failure_code: str | None,
    now: datetime,
) -> bool:
    """Fenced write of the provisioning outcome (only the current token's holder may write)."""
    statement = (
        update(_provisions)
        .where(
            _provisions.c.identity_id == identity_id,
            _provisions.c.provider_account_id == provider_account_id,
            _provisions.c.attempt_id == attempt_id,
            _provisions.c.status == ProvisionStatus.REQUESTED.value,
        )
        .values(
            status=status.value,
            failure_code=None if failure_code is None else failure_code[:100],
            updated_at=now,
        )
        .returning(_provisions.c.identity_id)
    )
    return (await session.execute(statement)).first() is not None
