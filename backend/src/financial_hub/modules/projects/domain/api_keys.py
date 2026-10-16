"""API keys: format, parsing, secret hashing and scopes (pure; docs/SECURITY.md).

A key looks like ``fh_<env>_<prefix>_<secret>``: ``env`` is ``test`` or ``live``, ``prefix`` is
8 public characters used only to find the row (and shown in lists so keys can be told apart), and
``secret`` is 32 random bytes in base62 (43 characters, ~256 bits). Only
``HMAC-SHA256(pepper, secret)`` is stored: because the secret is already high-entropy a fast keyed
hash is the right tool (no stretching needed), and the pepper, held outside the database, means a
database read alone can neither verify a guess nor mount an offline attack on a stolen digest.
"""

import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Final, Literal
from uuid import UUID

KEY_MARK: Final = "fh"
PREFIX_LENGTH: Final = 8
SECRET_BYTES: Final = 32
SECRET_LENGTH: Final = 43  # base62 digits needed for 32 bytes (62**43 > 2**256)
DIGEST_BYTES: Final = 32
NAME_MAX_LENGTH: Final = 100
BASE62: Final = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
PREFIX_ALPHABET: Final = "abcdefghijklmnopqrstuvwxyz0123456789"

type KeyEnvironment = Literal["test", "live"]

_KEY_PATTERN: Final = re.compile(
    rf"{KEY_MARK}_(?P<env>test|live)_(?P<prefix>[a-z0-9]{{{PREFIX_LENGTH}}})"
    rf"_(?P<secret>[0-9A-Za-z]{{{SECRET_LENGTH}}})"
)
# Verified against when the prefix is unknown, so a miss costs the same as a wrong secret.
DUMMY_DIGEST: Final = bytes(DIGEST_BYTES)


class ApiKeyScope(StrEnum):
    """What a key may do; the permissions each grants are mapped in ``application.scopes``."""

    PROJECTS_READ = "projects:read"
    CUSTOMERS_READ = "customers:read"
    CUSTOMERS_WRITE = "customers:write"
    LEDGER_READ = "ledger:read"
    PAYMENTS_READ = "payments:read"
    PAYMENTS_WRITE = "payments:write"
    PAYMENTS_REFUND = "payments:refund"
    BILLING_READ = "billing:read"
    BILLING_WRITE = "billing:write"
    EVENTS_READ = "events:read"


@dataclass(frozen=True, slots=True)
class ParsedKey:
    environment: KeyEnvironment
    prefix: str
    secret: str


@dataclass(frozen=True, slots=True)
class NewKey:
    """A freshly minted key. ``plaintext`` exists only here and in the one create response."""

    environment: KeyEnvironment
    prefix: str
    secret: str

    @property
    def plaintext(self) -> str:
        return f"{KEY_MARK}_{self.environment}_{self.prefix}_{self.secret}"

    def __repr__(self) -> str:
        return f"NewKey(environment={self.environment!r}, prefix={self.prefix!r}, secret=<hidden>)"


@dataclass(frozen=True, slots=True)
class ApiKey:
    id: UUID
    project_id: UUID
    name: str
    environment: KeyEnvironment
    prefix: str
    scopes: tuple[ApiKeyScope, ...]
    created_by: UUID
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None

    def is_usable(self, now: datetime) -> bool:
        return self.revoked_at is None and (self.expires_at is None or self.expires_at > now)


def _base62(number: int, length: int) -> str:
    digits: list[str] = []
    while number:
        number, remainder = divmod(number, len(BASE62))
        digits.append(BASE62[remainder])
    return "".join(reversed(digits)).rjust(length, BASE62[0])


def mint_key(environment: KeyEnvironment) -> NewKey:
    """A new random key (CSPRNG); the caller stores ``hash_secret(pepper, key.secret)``."""
    prefix = "".join(secrets.choice(PREFIX_ALPHABET) for _ in range(PREFIX_LENGTH))
    secret = _base62(int.from_bytes(secrets.token_bytes(SECRET_BYTES)), SECRET_LENGTH)
    return NewKey(environment=environment, prefix=prefix, secret=secret)


def parse_key(value: str) -> ParsedKey | None:
    """``None`` for anything that is not a well-formed key (never raises or echoes ``value``)."""
    match = _KEY_PATTERN.fullmatch(value)
    if match is None:
        return None
    environment: KeyEnvironment = "live" if match["env"] == "live" else "test"
    return ParsedKey(environment=environment, prefix=match["prefix"], secret=match["secret"])


def hash_secret(pepper: bytes, secret: str) -> bytes:
    return hmac.new(pepper, secret.encode("ascii"), sha256).digest()


def secret_matches(pepper: bytes, secret: str, stored_digest: bytes) -> bool:
    """Constant-time comparison of the presented secret against the stored digest."""
    return hmac.compare_digest(hash_secret(pepper, secret), stored_digest)


def validate_key_name(name: str) -> str:
    value = name.strip()
    if not 1 <= len(value) <= NAME_MAX_LENGTH:
        raise ValueError(f"name must have 1 to {NAME_MAX_LENGTH} characters")
    return value


def validate_scopes(scopes: list[str]) -> tuple[ApiKeyScope, ...]:
    """Distinct known scopes (order preserved); at least one is required."""
    if not scopes:
        raise ValueError("at least one scope is required")
    parsed: list[ApiKeyScope] = []
    for raw in scopes:
        try:
            scope = ApiKeyScope(raw)
        except ValueError:
            raise ValueError(f"unknown scope {raw!r}") from None
        if scope not in parsed:
            parsed.append(scope)
    return tuple(parsed)
