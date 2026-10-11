"""Event log domain (pure): stored events, cursors, delivery states, retry and schedule maths."""

import base64
import binascii
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Final, cast
from uuid import UUID

from financial_hub.shared.ids import IdPrefix, format_id
from financial_hub.shared.pagination import InvalidCursorError

EVENT_TYPE_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
"""``<context>.<name>[.<more>]``: lowercase dotted words (also enforced by a CHECK)."""
TYPE_FILTER_PATTERN: Final = re.compile(r"^(\*|[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*(\.\*)?)$")
"""A subscription filter: an exact type, ``<prefix>.*`` or ``*``."""
NAME_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
"""Handler and schedule names (also enforced by CHECKs)."""

type JsonObject = dict[str, Any]


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    SUCCEEDED = "succeeded"
    RETRY_SCHEDULED = "retry_scheduled"
    DEAD = "dead"


REPLAYABLE: Final = frozenset({DeliveryStatus.DEAD, DeliveryStatus.SUCCEEDED})


class SubscriptionKind(StrEnum):
    INTERNAL = "internal"
    WEBHOOK = "webhook"


@dataclass(frozen=True, slots=True)
class Event:
    """One row of the event log. ``txid`` orders events by commit visibility (ADR-0005)."""

    id: UUID
    type: str
    version: int
    project_id: UUID
    subject: str | None
    occurred_at: datetime
    correlation_id: UUID | None
    causation_id: UUID | None
    traceparent: str | None
    data: Mapping[str, Any]
    txid: int

    @property
    def public_id(self) -> str:
        return format_id(IdPrefix.EVENT, self.id)

    @property
    def cursor(self) -> EventCursor:
        return EventCursor(self.txid, self.id)


@dataclass(frozen=True, slots=True, order=True)
class EventCursor:
    """Position after an event in ``(txid, id)`` order (the only safe order for consumers)."""

    txid: int
    id: UUID

    def encode(self) -> str:
        """Opaque base64url (unpadded) JSON ``[txid, uuid]``."""
        payload = json.dumps([self.txid, str(self.id)], separators=(",", ":"))
        return base64.urlsafe_b64encode(payload.encode()).rstrip(b"=").decode("ascii")

    @classmethod
    def decode(cls, value: str) -> EventCursor:
        """Inverse of :meth:`encode`; malformed input raises ``InvalidCursorError``."""
        try:
            raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
            decoded: object = json.loads(raw)
            if not isinstance(decoded, list):
                raise InvalidCursorError
            items = cast(list[object], decoded)
            if len(items) != 2:
                raise InvalidCursorError
            txid, row_id = items
            if not isinstance(txid, int) or isinstance(txid, bool) or txid < 0:
                raise InvalidCursorError
            if not isinstance(row_id, str):
                raise InvalidCursorError
            return cls(txid, UUID(row_id))
        except binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError:
            raise InvalidCursorError from None


def is_valid_type_filter(value: str) -> bool:
    return TYPE_FILTER_PATTERN.fullmatch(value) is not None


def matches(event_type: str, filters: tuple[str, ...] | list[str]) -> bool:
    """Python mirror of the fan-out predicate in SQL (``*``, exact, ``prefix.*``)."""
    return any(
        f == "*" or f == event_type or (f.endswith(".*") and event_type.startswith(f[:-1]))
        for f in filters
    )


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Exponential backoff with full jitter; after ``max_attempts`` a delivery is ``dead``."""

    base_seconds: float = 1.0
    cap_seconds: float = 3600.0
    max_attempts: int = 10

    def __post_init__(self) -> None:
        if self.base_seconds <= 0 or self.cap_seconds < self.base_seconds:
            raise ValueError("retry policy needs 0 < base_seconds <= cap_seconds")
        if self.max_attempts < 1:
            raise ValueError("retry policy needs max_attempts >= 1")

    def ceiling(self, attempt: int) -> timedelta:
        """Upper bound of the delay after the ``attempt``-th failure (1-based)."""
        exponent = min(max(attempt, 1) - 1, 62)
        return timedelta(seconds=min(self.cap_seconds, self.base_seconds * 2.0**exponent))

    def delay(self, attempt: int, uniform: Callable[[], float]) -> timedelta:
        """Full jitter: ``uniform() * ceiling``, with ``uniform`` drawing from ``[0, 1)``."""
        return self.ceiling(attempt) * min(max(uniform(), 0.0), 1.0)

    def exhausted(self, attempts: int) -> bool:
        return attempts >= self.max_attempts


def due_runs(
    next_due_at: datetime, interval: timedelta, now: datetime, limit: int
) -> tuple[list[datetime], datetime]:
    """Every due instant ``<= now`` from ``next_due_at`` (at most ``limit``) and the next one."""
    if interval <= timedelta(0):
        raise ValueError("interval must be positive")
    runs: list[datetime] = []
    due = next_due_at
    while due <= now and len(runs) < limit:
        runs.append(due)
        due += interval
    return runs, due


def truncate_error(message: str, limit: int = 2000) -> str:
    return message if len(message) <= limit else message[: limit - 3] + "..."


def require_name(kind: str, value: str) -> str:
    if NAME_PATTERN.fullmatch(value) is None:
        raise ValueError(f"invalid {kind} name {value!r}")
    return value


@dataclass(frozen=True, slots=True)
class ClaimedDelivery:
    """A delivery leased by one dispatcher; only ``lease_token`` may complete it (fencing)."""

    id: UUID
    event: Event
    subscription_id: UUID
    handler: str
    lease_token: UUID
    lease_until: datetime
    attempts: int
