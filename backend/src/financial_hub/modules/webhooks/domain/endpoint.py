"""Webhook endpoints (pure): target URL rules, the retry schedule and the endpoint record."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final
from urllib.parse import SplitResult, urlsplit
from uuid import UUID

from financial_hub.modules.webhooks.domain.ssrf import parse_ip_host, refusal_reason

MAX_URL_LENGTH: Final = 2048
MAX_DESCRIPTION_LENGTH: Final = 500
MAX_EVENT_TYPES: Final = 50
SECRET_OVERLAP: Final = timedelta(hours=24)
"""How long the previous secret keeps signing after a rotation."""


class InvalidWebhookUrlError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TargetPolicy:
    """Where webhooks may go. ``allow_http`` and ``allowed_hosts`` are honoured only in dev/test.

    ``allowed_hosts`` (exact, case-insensitive names or IP literals) skip the private-address
    check so tests and local e2e can use a receiver on localhost or the compose network.
    """

    allow_http: bool = False
    allowed_hosts: frozenset[str] = frozenset()

    @classmethod
    def for_environment(cls, *, dev: bool, allowed_hosts: frozenset[str]) -> TargetPolicy:
        if not dev:
            return cls()
        return cls(allow_http=True, allowed_hosts=frozenset(h.lower() for h in allowed_hosts))

    def is_allowlisted(self, host: str) -> bool:
        return host.lower() in self.allowed_hosts


def validate_url(url: str, policy: TargetPolicy) -> SplitResult:
    """Syntactic checks plus an early refusal of private IP literals (names are checked at send)."""
    if not 1 <= len(url) <= MAX_URL_LENGTH or not url.isascii():
        raise InvalidWebhookUrlError(f"url must be 1..{MAX_URL_LENGTH} ASCII characters")
    if any(ch.isspace() or ord(ch) < 0x20 for ch in url):
        raise InvalidWebhookUrlError("url must not contain whitespace or control characters")
    parts = urlsplit(url)
    allowed = ("https", "http") if policy.allow_http else ("https",)
    if parts.scheme not in allowed:
        raise InvalidWebhookUrlError(f"url scheme must be {' or '.join(allowed)}")
    if parts.username is not None or parts.password is not None:
        raise InvalidWebhookUrlError("url must not contain credentials")
    if parts.fragment:
        raise InvalidWebhookUrlError("url must not contain a fragment")
    host = parts.hostname
    if not host:
        raise InvalidWebhookUrlError("url must have a host")
    try:
        _ = parts.port
    except ValueError:
        raise InvalidWebhookUrlError("url has an invalid port") from None
    if not policy.is_allowlisted(host):
        address = parse_ip_host(host)
        if address is not None:
            reason = refusal_reason(address)
            if reason is not None:
                raise InvalidWebhookUrlError(f"url host is not allowed ({reason})")
        elif host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise InvalidWebhookUrlError("url host is not allowed (local name)")
    return parts


@dataclass(frozen=True, slots=True)
class RetrySchedule:
    """Exponential-ish backoff with +-20% jitter: ~30 s, 2 min, 10 min, 30 min, 2 h, 8 h, 24 h.

    Attempt ``n`` (1-based) failing waits ``delays[n - 1]``; the ``max_attempts``-th failure
    makes the delivery ``dead`` (about 35 h after the first attempt).
    """

    delays_seconds: tuple[int, ...] = (30, 120, 600, 1800, 7200, 28800, 86400)
    jitter: float = 0.2

    @property
    def max_attempts(self) -> int:
        return len(self.delays_seconds) + 1

    def exhausted(self, attempts: int) -> bool:
        return attempts >= self.max_attempts

    def delay(self, attempt: int, uniform: Callable[[], float]) -> timedelta:
        """Delay after the ``attempt``-th failure; ``uniform`` draws from ``[0, 1)``."""
        base = self.delays_seconds[min(max(attempt, 1), len(self.delays_seconds)) - 1]
        factor = 1 - self.jitter + 2 * self.jitter * min(max(uniform(), 0.0), 1.0)
        return timedelta(seconds=base * factor)


@dataclass(frozen=True, slots=True)
class WebhookEndpoint:
    id: UUID
    project_id: UUID
    subscription_id: UUID
    url: str
    description: str | None
    event_types: tuple[str, ...]
    enabled: bool
    secret_key_version: int
    previous_expires_at: datetime | None
    created_at: datetime
    updated_at: datetime
    disabled_at: datetime | None

    def overlapping(self, now: datetime) -> bool:
        """Whether the previous secret still signs (rotation overlap)."""
        return self.previous_expires_at is not None and now < self.previous_expires_at
