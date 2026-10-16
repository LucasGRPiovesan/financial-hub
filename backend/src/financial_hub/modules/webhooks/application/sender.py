"""Webhook sender: leases webhook deliveries from the event log and POSTs them (W6.3).

Per delivery: load the endpoint, decrypt its signing secret(s), sign the event envelope (the
same JSON the event explorer shows) with Standard Webhooks headers, send it through the
SSRF-guarded client, then record the attempt and complete the delivery in one transaction
(lease-token fencing, as in the internal dispatcher). 2xx = delivered; anything else is
retried on :class:`RetrySchedule` until the last attempt, then the delivery is ``dead``.

The sender polls (no LISTEN): webhook latency of a poll interval is acceptable and keeps the
worker's notification channel to the internal dispatcher.
"""

import asyncio
import contextlib
import random
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

import structlog
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from financial_hub.modules.events.application import (
    ClaimedWebhookDelivery,
    claim_webhook_deliveries,
    complete_webhook_delivered,
    complete_webhook_failed,
    release_webhook_delivery,
)
from financial_hub.modules.webhooks.domain import RetrySchedule, signature_header
from financial_hub.modules.webhooks.infrastructure import repository
from financial_hub.modules.webhooks.infrastructure.http_client import (
    GuardedSender,
    SendOutcome,
    SendResult,
)
from financial_hub.modules.webhooks.infrastructure.secret_vault import (
    SecretUnavailableError,
    signing_secrets,
)
from financial_hub.platform.crypto.keyring import Keyring
from financial_hub.platform.db import UnitOfWork
from financial_hub.shared.clock import Clock
from financial_hub.shared.ids import IdPrefix, format_id, new_uuid

logger = structlog.get_logger()

MAX_ERROR_LENGTH = 2000


class _FencedError(Exception):
    """The delivery was re-leased by another sender: drop this attempt."""


class SendStatus(StrEnum):
    DELIVERED = "delivered"
    RETRY_SCHEDULED = "retry_scheduled"
    DEAD = "dead"
    RELEASED = "released"
    FENCED = "fenced"


class WebhookEnvelope(BaseModel):
    """The event as ``GET /v1/projects/{id}/events/{event_id}`` shows it."""

    id: str
    type: str
    version: int
    project_id: str
    subject: str | None
    occurred_at: datetime
    recorded_at: datetime
    correlation_id: str | None
    causation_id: str | None
    traceparent: str | None
    txid: int
    data: dict[str, Any]

    @classmethod
    def of(cls, delivery: ClaimedWebhookDelivery) -> WebhookEnvelope:
        event = delivery.event
        return cls(
            id=event.public_id,
            type=event.type,
            version=event.version,
            project_id=format_id(IdPrefix.PROJECT, event.project_id),
            subject=event.subject,
            occurred_at=event.occurred_at,
            recorded_at=delivery.recorded_at,
            correlation_id=None if event.correlation_id is None else str(event.correlation_id),
            causation_id=(
                None
                if event.causation_id is None
                else format_id(IdPrefix.EVENT, event.causation_id)
            ),
            traceparent=event.traceparent,
            txid=event.txid,
            data=dict(event.data),
        )


def truncate(error: str, limit: int = MAX_ERROR_LENGTH) -> str:
    return error if len(error) <= limit else error[: limit - 3] + "..."


class WebhookSender:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        keyring: Keyring,
        clock: Clock,
        client: GuardedSender,
        schedule: RetrySchedule | None = None,
        batch_size: int = 10,
        lease_seconds: float = 300.0,
        poll_interval_seconds: float = 2.0,
        uniform: Callable[[], float] = random.random,
    ) -> None:
        self._session_factory = session_factory
        self._keyring = keyring
        self._clock = clock
        self._client = client
        self._schedule = schedule or RetrySchedule()
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds
        self._poll_interval = poll_interval_seconds
        self._uniform = uniform

    async def claim(self) -> list[ClaimedWebhookDelivery]:
        now = self._clock.now()
        async with UnitOfWork(self._session_factory) as uow:
            return await claim_webhook_deliveries(
                uow,
                now=now,
                lease_until=now + timedelta(seconds=self._lease_seconds),
                limit=self._batch_size,
            )

    async def release(self, delivery: ClaimedWebhookDelivery) -> SendStatus:
        async with UnitOfWork(self._session_factory) as uow:
            released = await release_webhook_delivery(uow, delivery)
        return SendStatus.RELEASED if released else SendStatus.FENCED

    async def process(self, delivery: ClaimedWebhookDelivery) -> SendStatus:
        log = logger.bind(
            delivery_id=format_id(IdPrefix.DELIVERY, delivery.id),
            event_id=delivery.event.public_id,
            endpoint_id=format_id(IdPrefix.WEBHOOK_ENDPOINT, delivery.endpoint_id),
            attempt=delivery.attempts,
        )
        now = self._clock.now()
        async with UnitOfWork(self._session_factory) as uow:
            target = await repository.sending_target(uow.session, delivery.endpoint_id)
            if target is None or not target.enabled:
                secrets: list[str] = []
            else:
                try:
                    secrets = await signing_secrets(
                        uow.session, self._keyring, delivery.endpoint_id, now
                    )
                except SecretUnavailableError:
                    secrets = []
        if target is None or not target.enabled:
            # Disabled meanwhile: the claim filters on the subscription, so this is a race.
            # Give the lease back; the delivery waits until the endpoint is enabled again.
            return await self.release(delivery)
        if delivery.attempts > self._schedule.max_attempts:
            return await self._complete(
                delivery,
                target.project_id,
                SendResult(SendOutcome.FAILED, None, 0, "lease expired after the last attempt"),
                record=False,
            )
        if not secrets:
            result = SendResult(
                SendOutcome.FAILED, None, 0, "signing secret unavailable (encryption key missing)"
            )
            return await self._complete(delivery, target.project_id, result)
        body = WebhookEnvelope.of(delivery).model_dump_json().encode()
        message_id = delivery.event.public_id
        timestamp = int(now.timestamp())
        headers = {
            "Content-Type": "application/json",
            "webhook-id": message_id,
            "webhook-timestamp": str(timestamp),
            "webhook-signature": signature_header(
                secrets, message_id=message_id, timestamp=timestamp, body=body
            ),
        }
        del secrets
        result = await self._client.post(target.url, headers=headers, body=body)
        status = await self._complete(delivery, target.project_id, result)
        log.info(
            "webhook.delivery_attempted",
            outcome=result.outcome.value,
            status_code=result.status_code,
            latency_ms=result.latency_ms,
            status=status.value,
        )
        return status

    async def _complete(
        self,
        delivery: ClaimedWebhookDelivery,
        project_id: UUID,
        result: SendResult,
        *,
        record: bool = True,
    ) -> SendStatus:
        now = self._clock.now()
        error = None if result.error is None else truncate(result.error)
        try:
            async with UnitOfWork(self._session_factory) as uow:
                status = await self._record_and_complete(
                    uow, delivery, project_id, result, error, now, record=record
                )
        except _FencedError:  # lease lost: the attempt row rolls back with the transaction
            status = SendStatus.FENCED
        if status is SendStatus.FENCED:
            logger.warning(
                "webhook.delivery_fenced", delivery_id=format_id(IdPrefix.DELIVERY, delivery.id)
            )
        elif status is SendStatus.DEAD:
            logger.error(
                "webhook.delivery_dead",
                delivery_id=format_id(IdPrefix.DELIVERY, delivery.id),
                endpoint_id=format_id(IdPrefix.WEBHOOK_ENDPOINT, delivery.endpoint_id),
                attempts=delivery.attempts,
                error=None if error is None else truncate(error, 500),
            )
        return status

    async def _record_and_complete(
        self,
        uow: UnitOfWork,
        delivery: ClaimedWebhookDelivery,
        project_id: UUID,
        result: SendResult,
        error: str | None,
        now: datetime,
        *,
        record: bool,
    ) -> SendStatus:
        if record:
            await repository.insert_attempt(
                uow.session,
                attempt_id=new_uuid(),
                project_id=project_id,
                endpoint_id=delivery.endpoint_id,
                delivery_id=delivery.id,
                attempt=delivery.attempts,
                outcome=result.outcome.value,
                status_code=result.status_code,
                latency_ms=result.latency_ms,
                error=error,
                attempted_at=now,
            )
        if result.delivered:
            completed = await complete_webhook_delivered(uow, delivery)
            status = SendStatus.DELIVERED
        else:
            dead = self._schedule.exhausted(delivery.attempts)
            next_attempt_at = (
                now if dead else now + self._schedule.delay(delivery.attempts, self._uniform)
            )
            completed = await complete_webhook_failed(
                uow,
                delivery,
                dead=dead,
                next_attempt_at=next_attempt_at,
                error=error or "failed",
            )
            status = SendStatus.DEAD if dead else SendStatus.RETRY_SCHEDULED
        if not completed:
            raise _FencedError
        return status

    async def run_once(self, stop: asyncio.Event | None = None) -> list[SendStatus]:
        claimed = await self.claim()
        statuses: list[SendStatus] = []
        for index, delivery in enumerate(claimed):
            if stop is not None and stop.is_set():
                for remaining in claimed[index:]:
                    statuses.append(await self.release(remaining))
                break
            if self._clock.now() >= delivery.lease_until:
                statuses.append(await self.release(delivery))
                continue
            statuses.append(await self.process(delivery))
        return statuses

    async def run(self, stop: asyncio.Event) -> None:
        logger.info("webhook_sender.started", batch_size=self._batch_size)
        try:
            while not stop.is_set():
                processed = 0
                started = time.monotonic()
                try:
                    processed = len(await self.run_once(stop))
                except Exception:
                    logger.exception("webhook_sender.iteration_failed")
                if processed >= self._batch_size or stop.is_set():
                    continue
                remaining = max(self._poll_interval - (time.monotonic() - started), 0.05)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=remaining)
        finally:
            logger.info("webhook_sender.stopped")
