"""Field rules of a customer identity (pure). Messages never repeat the offending value (PII)."""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from financial_hub.modules.customers.domain.documents import CustomerKind

EXTERNAL_REF_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+=~-]{0,127}$")
NAME_MAX_LENGTH: Final = 200
EMAIL_MAX_LENGTH: Final = 254
METADATA_MAX_KEYS: Final = 50
METADATA_MAX_KEY_LENGTH: Final = 40
METADATA_MAX_BYTES: Final = 4096
_EMAIL: Final = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]+$")
_PHONE_FORMATTING: Final = re.compile(r"[\s().-]")
_PHONE: Final = re.compile(r"^\+?[0-9]{8,15}$")


def validate_external_ref(value: str) -> str:
    if EXTERNAL_REF_PATTERN.fullmatch(value) is None:
        raise ValueError(
            "external_ref must be 1-128 characters: letters, digits and . _ : @ + = ~ -"
        )
    return value


def validate_name(value: str) -> str:
    cleaned = value.strip()
    if not 1 <= len(cleaned) <= NAME_MAX_LENGTH:
        raise ValueError(f"name must have 1..{NAME_MAX_LENGTH} characters")
    return cleaned


def normalize_email(value: str) -> str:
    cleaned = value.strip().lower()
    if len(cleaned) > EMAIL_MAX_LENGTH or _EMAIL.fullmatch(cleaned) is None:
        raise ValueError("email is not valid")
    return cleaned


def normalize_phone(value: str) -> str:
    cleaned = _PHONE_FORMATTING.sub("", value)
    if _PHONE.fullmatch(cleaned) is None:
        raise ValueError("phone must have 8 to 15 digits, optionally starting with +")
    return cleaned


def validate_metadata(value: Mapping[str, Any]) -> dict[str, Any]:
    if len(value) > METADATA_MAX_KEYS:
        raise ValueError(f"metadata allows at most {METADATA_MAX_KEYS} keys")
    if any(not key or len(key) > METADATA_MAX_KEY_LENGTH for key in value):
        raise ValueError(f"metadata keys must have 1..{METADATA_MAX_KEY_LENGTH} characters")
    try:
        encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    except TypeError, ValueError:
        raise ValueError("metadata must be JSON-serialisable") from None
    if len(encoded.encode()) > METADATA_MAX_BYTES:
        raise ValueError(f"metadata must be at most {METADATA_MAX_BYTES} bytes")
    return dict(value)


@dataclass(frozen=True, slots=True)
class CustomerIdentity:
    """A project's view of a customer. ``financial_customer_id`` is internal: never serialised."""

    id: UUID
    project_id: UUID
    financial_customer_id: UUID
    external_ref: str
    kind: CustomerKind
    name: str
    email: str | None
    phone: str | None
    metadata: dict[str, Any]
    document_last4: str | None
    anonymized_at: datetime | None
    created_at: datetime
    updated_at: datetime
