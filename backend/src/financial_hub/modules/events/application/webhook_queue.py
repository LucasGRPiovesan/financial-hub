"""Webhook delivery queue: what the webhooks module's sender needs from the event log (W6.3).

Webhook subscriptions fan out like internal ones; their deliveries are leased here with the same
lease-token fencing as :mod:`dispatcher` (claim in its own transaction, complete by
compare-and-set). The HTTP side lives in ``financial_hub.modules.webhooks``.
"""

from datetime import datetime
from uuid import UUID

from financial_hub.modules.events.domain import ClaimedWebhookDelivery, DeliveryStatus
from financial_hub.modules.events.infrastructure import repository
from financial_hub.platform.db import UnitOfWork


async def claim_webhook_deliveries(
    uow: UnitOfWork, *, now: datetime, lease_until: datetime, limit: int
) -> list[ClaimedWebhookDelivery]:
    return await repository.claim_webhook_deliveries(
        uow.session, now=now, lease_until=lease_until, limit=limit
    )


async def complete_webhook_delivered(uow: UnitOfWork, delivery: ClaimedWebhookDelivery) -> bool:
    """``False`` when the lease was lost (another sender re-leased the delivery)."""
    return await repository.complete_succeeded(uow.session, delivery.id, delivery.lease_token)


async def complete_webhook_failed(
    uow: UnitOfWork,
    delivery: ClaimedWebhookDelivery,
    *,
    dead: bool,
    next_attempt_at: datetime,
    error: str,
) -> bool:
    return await repository.complete_failed(
        uow.session,
        delivery.id,
        delivery.lease_token,
        status=DeliveryStatus.DEAD if dead else DeliveryStatus.RETRY_SCHEDULED,
        next_attempt_at=next_attempt_at,
        error=error,
    )


async def release_webhook_delivery(uow: UnitOfWork, delivery: ClaimedWebhookDelivery) -> bool:
    """Give an unsent lease back (shutdown) without spending its attempt."""
    return await repository.release_lease(uow.session, delivery.id, delivery.lease_token)


async def webhook_delivery_endpoint(
    uow: UnitOfWork, *, project_id: UUID, event_id: UUID, delivery_id: UUID
) -> UUID | None:
    return await repository.webhook_delivery_endpoint(
        uow.session, project_id=project_id, event_id=event_id, delivery_id=delivery_id
    )
