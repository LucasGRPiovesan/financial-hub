"""Payment entities (pure): PaymentIntent, ProviderPayment, Refund, outbound attempts.

Amounts are :class:`Money` (integer minor units) at the edges and plain ``int`` minor units in the
state fields; a float never enters this module.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Final
from uuid import UUID

from financial_hub.modules.payments.domain.refunds import RefundState
from financial_hub.modules.payments.domain.state import CreationStatus, PaymentState
from financial_hub.shared.money import Money

FAKE_PROVIDER: Final = "fake"
EXTERNAL_REF_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+=~-]{0,127}")
METADATA_MAX_BYTES: Final = 8192
DESCRIPTION_MAX_LENGTH: Final = 500


class PaymentMethod(StrEnum):
    PIX = "pix"
    BOLETO = "boleto"
    CARD = "card"


class AttemptKind(StrEnum):
    CREATE_PAYMENT = "create_payment"
    REFUND = "refund"


class AttemptStatus(StrEnum):
    """One outbound provider call (ADR-0012 s3). ``id`` is the fencing token."""

    REQUESTED = "requested"  # committed before the call
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    UNKNOWN = "unknown"  # timeout/5xx/crash: only a lookup resolves it
    ABSENT = "absent"  # the lookup confirmed nothing exists: a new attempt may be made
    SUPERSEDED = "superseded"  # another writer (the resolver) recorded the outcome first


@dataclass(frozen=True, slots=True)
class PaymentIntent:
    id: UUID
    project_id: UUID
    customer_id: UUID
    amount: Money
    method: PaymentMethod
    due_date: date
    external_ref: str | None
    description: str | None
    metadata: Mapping[str, Any]
    provider: str
    provider_account_id: UUID | None
    creation_status: CreationStatus
    attempt_id: UUID
    failure_code: str | None
    created_at: datetime
    updated_at: datetime
    # The invoice this intent collects (W5.3): set by billing only, never from the HTTP API.
    invoice_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ProviderPayment:
    id: UUID
    payment_intent_id: UUID
    project_id: UUID
    provider: str
    provider_account_id: UUID | None
    provider_payment_id: str
    raw_status: str
    state: PaymentState
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Refund:
    id: UUID
    project_id: UUID
    payment_intent_id: UUID
    provider_payment_id: UUID
    amount: Money
    status: RefundState
    reason: str | None
    provider_refund_id: str | None
    attempt_id: UUID | None
    origin: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Attempt:
    id: UUID
    project_id: UUID
    kind: AttemptKind
    payment_intent_id: UUID
    refund_id: UUID | None
    status: AttemptStatus
    request_hash: bytes
    created_at: datetime


def validate_external_ref(value: str) -> str:
    if EXTERNAL_REF_PATTERN.fullmatch(value) is None:
        raise ValueError(
            "must be 1-128 characters: letters, digits and . _ : @ + = ~ - "
            "(starting with a letter or digit)"
        )
    return value


def validate_amount(amount: Money) -> Money:
    if not amount.is_positive():
        raise ValueError("must be a positive amount in minor units")
    return amount


def validate_metadata(value: Mapping[str, Any]) -> dict[str, Any]:
    """A flat-or-nested JSON object of at most ``METADATA_MAX_BYTES`` (never PII by contract)."""
    encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    if len(encoded.encode("utf-8")) > METADATA_MAX_BYTES:
        raise ValueError(f"must be at most {METADATA_MAX_BYTES} bytes of JSON")
    return dict(value)


def validate_description(value: str | None) -> str | None:
    if value is None:
        return None
    clean = value.strip()
    if not clean or len(clean) > DESCRIPTION_MAX_LENGTH:
        raise ValueError(f"must be 1-{DESCRIPTION_MAX_LENGTH} characters")
    return clean
