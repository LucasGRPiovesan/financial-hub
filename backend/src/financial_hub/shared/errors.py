"""Domain error hierarchy. Transport-agnostic; ``platform.http.problems`` maps it to RFC 9457.

``slug`` is the stable machine identifier (``https://financial-hub.dev/problems/<slug>``), part of
the public API contract: never rename one. Subclass an existing error to get its mapping for free.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar


@dataclass(frozen=True, slots=True)
class FieldError:
    """One invalid input: ``pointer`` is an RFC 6901 JSON pointer into the request body."""

    pointer: str
    detail: str
    code: str | None = None


class DomainError(Exception):
    slug: ClassVar[str] = "domain-error"
    title: ClassVar[str] = "Domain error"

    def __init__(self, detail: str | None = None, *, errors: Sequence[FieldError] = ()) -> None:
        super().__init__(detail or self.title)
        self.detail = detail
        self.errors: tuple[FieldError, ...] = tuple(errors)


class NotFound(DomainError):
    slug = "not-found"
    title = "Resource not found"


class Conflict(DomainError):
    slug = "conflict"
    title = "Conflict with the current state of the resource"


class ValidationFailed(DomainError):
    slug = "validation-failed"
    title = "Request validation failed"


class Forbidden(DomainError):
    slug = "forbidden"
    title = "Forbidden"


class Unauthorized(DomainError):
    slug = "unauthorized"
    title = "Authentication required"


class IdempotencyConflict(Conflict):
    """A request with the same ``Idempotency-Key`` is still in flight (ADR-0004)."""

    slug = "idempotency-conflict"
    title = "A request with this idempotency key is in progress"


class IdempotencyKeyReused(DomainError):
    """The ``Idempotency-Key`` was already used with a different payload (ADR-0004: 422)."""

    slug = "idempotency-key-reused"
    title = "Idempotency key reused with a different request"


class InsufficientFunds(DomainError):
    slug = "insufficient-funds"
    title = "Insufficient funds"


class RateLimited(DomainError):
    slug = "rate-limited"
    title = "Too many requests"

    def __init__(
        self, detail: str | None = None, *, retry_after_seconds: int | None = None
    ) -> None:
        super().__init__(detail)
        self.retry_after_seconds = retry_after_seconds
