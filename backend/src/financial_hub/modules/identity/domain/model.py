"""Operators, sessions and password policy (pure rules; persistence lives in infrastructure)."""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final, Protocol
from uuid import UUID

PASSWORD_MIN_LENGTH: Final = 12
PASSWORD_MAX_LENGTH: Final = 128
EMAIL_MAX_LENGTH: Final = 254
DISPLAY_NAME_MAX_LENGTH: Final = 100
USER_AGENT_MAX_LENGTH: Final = 512
# The demo login (FH_DEMO_LOGIN) only serves operators in this domain (the `seed-demo` personas).
DEMO_EMAIL_DOMAIN: Final = "demo.financial-hub.local"
# Deliberately loose (one @, a dot in the domain, no spaces): ownership is proven out of band.
_EMAIL: Final = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def is_demo_email(email: str) -> bool:
    """Whether `email` belongs to the demo personas' domain (case-insensitive)."""
    return email.rpartition("@")[2].casefold() == DEMO_EMAIL_DOMAIN


class OperatorStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class PasswordPolicyError(ValueError):
    """The new password does not satisfy the policy."""


class PasswordHasher(Protocol):
    """Password hashing port (Argon2id in infrastructure)."""

    def hash(self, password: str) -> str: ...

    def verify(self, password_hash: str, password: str) -> bool:
        """Constant-time verification; ``False`` (never an exception) on mismatch."""
        ...

    def needs_rehash(self, password_hash: str) -> bool: ...

    @property
    def dummy_hash(self) -> str:
        """A valid hash of a random secret: verified when the email is unknown (same timing)."""
        ...


@dataclass(frozen=True, slots=True)
class Operator:
    id: UUID
    email: str
    display_name: str
    password_hash: str
    status: OperatorStatus
    failed_login_count: int
    locked_until: datetime | None
    password_changed_at: datetime
    created_at: datetime

    def is_locked(self, now: datetime) -> bool:
        return self.locked_until is not None and now < self.locked_until


@dataclass(frozen=True, slots=True)
class SessionPolicy:
    idle_timeout: timedelta
    absolute_timeout: timedelta
    touch_interval: timedelta

    def expiries(self, created_at: datetime) -> tuple[datetime, datetime]:
        """``(idle_expires_at, absolute_expires_at)`` of a session created at ``created_at``."""
        absolute = created_at + self.absolute_timeout
        return min(created_at + self.idle_timeout, absolute), absolute


@dataclass(frozen=True, slots=True)
class Session:
    id: bytes
    operator_id: UUID
    csrf_token_hash: bytes
    created_at: datetime
    last_seen_at: datetime
    idle_expires_at: datetime
    absolute_expires_at: datetime
    revoked_at: datetime | None

    def is_active(self, now: datetime) -> bool:
        return (
            self.revoked_at is None
            and now < self.idle_expires_at
            and now < self.absolute_expires_at
        )

    def touch(self, now: datetime, policy: SessionPolicy) -> datetime | None:
        """New ``idle_expires_at`` when the sliding window should be written now, else ``None``.

        Writes are throttled to one per ``touch_interval`` so reads do not all become updates.
        """
        if now - self.last_seen_at < policy.touch_interval:
            return None
        return min(now + policy.idle_timeout, self.absolute_expires_at)


def normalize_email(email: str) -> str:
    """Case-insensitive identity: trimmed and lower-cased (uniqueness is on this form)."""
    return email.strip().lower()


def is_valid_email(email: str) -> bool:
    return len(email) <= EMAIL_MAX_LENGTH and _EMAIL.fullmatch(email) is not None


def mask_email(email: str) -> str:
    """Log-safe form: ``j***@example.com`` (first character of the local part only)."""
    local, separator, domain = email.partition("@")
    if not separator:
        return "***"
    return f"{local[:1]}***@{domain}"


def validate_new_password(password: str) -> str:
    if not PASSWORD_MIN_LENGTH <= len(password) <= PASSWORD_MAX_LENGTH:
        raise PasswordPolicyError(
            f"password must have {PASSWORD_MIN_LENGTH} to {PASSWORD_MAX_LENGTH} characters"
        )
    return password


def validate_display_name(display_name: str) -> str:
    value = display_name.strip()
    if not 1 <= len(value) <= DISPLAY_NAME_MAX_LENGTH:
        raise ValueError(f"display name must have 1 to {DISPLAY_NAME_MAX_LENGTH} characters")
    return value
