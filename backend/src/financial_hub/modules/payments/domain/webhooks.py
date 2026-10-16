"""Inbound provider notifications (W4.4, docs/INTEGRATIONS.md s2). Pure types only.

A notification is never trusted for money (ADR-0012 s2): it only says *which* provider payment
changed. Processing refetches that payment and applies the fetched snapshot through the state
transitions, so a forged amount, a replayed body or an out-of-order delivery cannot move money.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class WebhookAction(StrEnum):
    """What a notification type asks the hub to do."""

    REFRESH = "refresh"  # refetch the payment and apply it
    IGNORE = "ignore"  # documented, no effect on the hub's state
    PARK = "park"  # unknown or unsupported: stored for review, acknowledged


class WebhookStatus(StrEnum):
    RECEIVED = "received"
    PROCESSED = "processed"
    IGNORED = "ignored"
    PARKED = "parked"
    FAILED = "failed"


PENDING_WEBHOOK_STATES = frozenset({WebhookStatus.RECEIVED, WebhookStatus.FAILED})


class WebhookPayloadInvalid(ValueError):
    """The body is not a notification the provider documents (no event id or type)."""


@dataclass(frozen=True, slots=True)
class ParsedNotification:
    """The parts of a notification the hub uses (no amounts, no customer data)."""

    provider_event_id: str
    event_type: str
    action: WebhookAction
    provider_payment_id: str | None = None
    external_reference: str | None = None


@dataclass(frozen=True, slots=True)
class InboundWebhook:
    """One stored notification (``provider_webhook_events``). ``body`` is the raw request body."""

    id: UUID
    project_id: UUID
    provider: str
    provider_account_id: UUID
    provider_event_id: str
    event_type: str
    body: str
    status: WebhookStatus
    outcome: str | None
    attempts: int
    last_error: str | None
    received_at: datetime
    processed_at: datetime | None
    updated_at: datetime


class ProvisionStatus(StrEnum):
    """The customer-provisioning call of one ``(identity, provider account)`` (ADR-0012 s3)."""

    REQUESTED = "requested"  # a call may be in flight until ``lease_until``
    UNKNOWN = "unknown"  # the call returned without a definitive answer
    CREATED = "created"
    REJECTED = "rejected"
