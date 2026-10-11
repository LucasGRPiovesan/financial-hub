"""Login throttling rules: fixed-window counters per email and per client IP.

- ``email`` counts consecutive *failed* logins for a normalized email, whether or not an operator
  has it, so a 429 never reveals that an account exists. Reaching the limit blocks the email for
  ``block``; a successful login resets it.
- ``ip`` counts *every* attempt from one address (password spraying across many emails).

Keys are stored as SHA-256 digests, never as plain emails or addresses.
"""

import hashlib
import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum


class ThrottleScope(StrEnum):
    EMAIL = "email"
    IP = "ip"


@dataclass(frozen=True, slots=True)
class ThrottlePolicy:
    limit: int
    window: timedelta
    block: timedelta


@dataclass(frozen=True, slots=True)
class ThrottleState:
    window_started_at: datetime
    attempts: int
    blocked_until: datetime | None

    @classmethod
    def fresh(cls, now: datetime) -> ThrottleState:
        return cls(window_started_at=now, attempts=0, blocked_until=None)

    def retry_after(self, now: datetime) -> int | None:
        """Whole seconds until the block ends (at least 1), or ``None`` when not blocked."""
        if self.blocked_until is None or now >= self.blocked_until:
            return None
        return max(1, math.ceil((self.blocked_until - now).total_seconds()))

    def record(self, now: datetime, policy: ThrottlePolicy) -> ThrottleState:
        """Count one attempt/failure at ``now``; block once the window reaches the limit."""
        if now >= self.window_started_at + policy.window or self.blocked_until is not None:
            state = ThrottleState(window_started_at=now, attempts=1, blocked_until=None)
        else:
            state = replace(self, attempts=self.attempts + 1)
        if state.attempts >= policy.limit:
            state = replace(state, blocked_until=now + policy.block)
        return state


def throttle_key(scope: ThrottleScope, value: str) -> bytes:
    return hashlib.sha256(f"financial-hub/login/{scope.value}:{value}".encode()).digest()
