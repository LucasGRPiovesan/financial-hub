"""Rate limiting with Postgres token buckets (docs/SECURITY.md, Rate limiting row).

Buckets:

- ``api_key:<key id>`` and ``operator:<operator id>``: every request of an authenticated principal
  (``RateLimitMiddleware``), with ``RateLimit-Limit``/``RateLimit-Remaining`` on the response;
- ``auth_ip:<client ip>``: unauthenticated authentication attempts from one client IP, i.e.
  requests whose credentials an authenticator rejected (unknown/revoked API keys, dead sessions)
  and every login attempt (``auth_attempt()`` route dependency).

A denied request gets ``429`` problem+json (``.../rate-limited``) with an integer ``Retry-After``.
Health checks and the SSE stream (``EXEMPT_PATHS``) are never limited.

Each check is **one** ``INSERT ... ON CONFLICT DO UPDATE`` statement: the row lock taken by the
upsert serialises concurrent checks of one bucket, the refill (elapsed time x rate, capped at the
capacity) and the take are computed from the locked row, and ``RETURNING old.*/new.*``
(PostgreSQL 18) reports the decision. There is no read-modify-write window. Token arithmetic is
exact ``numeric`` in SQL and ``Decimal``/``int`` in Python, never ``float``.

Client IP: the ASGI peer address. ``X-Forwarded-For`` is used only when the peer is one of the
configured trusted proxies (``FH_RATE_LIMIT_TRUSTED_PROXIES``); then the right-most address that
is not a trusted proxy is the client.
"""

import ipaddress
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Final

import structlog
from fastapi import Depends, Request, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from financial_hub.platform.http.auth import Authenticator, Principal
from financial_hub.platform.http.problems import problem_response, problem_type
from financial_hub.platform.http.protection.short_circuit import ShortCircuit
from financial_hub.platform.settings import Settings
from financial_hub.shared.errors import DomainError, RateLimited, Unauthorized

LIMIT_HEADER: Final = "RateLimit-Limit"
REMAINING_HEADER: Final = "RateLimit-Remaining"
RETRY_AFTER_HEADER: Final = "Retry-After"
FORWARDED_FOR_HEADER: Final = "X-Forwarded-For"
API_KEY_KIND: Final = "api_key"
AUTH_IP_BUCKET: Final = "auth_ip"
UNKNOWN_IP: Final = "unknown"
HEADERS_STATE: Final = "rate_limit_headers"
# Never limited per request: liveness/readiness probes and the long-lived SSE stream.
EXEMPT_PATHS: Final = (re.compile(r"/health/.*"), re.compile(r"/v1/projects/[^/]+/stream"))

logger = structlog.get_logger()

type Network = ipaddress.IPv4Network | ipaddress.IPv6Network

# One statement per check. ``refilled`` is the locked row's balance brought up to ``now``; the take
# succeeds when it holds a whole token. ``updated_at`` never moves backwards (clock skew), so a
# late clock cannot mint tokens. A new bucket starts full and pays for this request.
_REFILLED = (
    "LEAST(CAST(:capacity AS numeric), {row}.tokens + GREATEST(0, EXTRACT(EPOCH FROM "
    "(CAST(:now AS timestamptz) - {row}.updated_at))) * CAST(:rate AS numeric))"
)
TAKE_TOKEN_SQL: Final = f"""
INSERT INTO rate_limit_buckets AS bucket (bucket_key, tokens, updated_at)
VALUES (:key, CAST(:capacity AS numeric) - 1, CAST(:now AS timestamptz))
ON CONFLICT (bucket_key) DO UPDATE SET
    tokens = {_REFILLED.format(row="bucket")}
        - CASE WHEN {_REFILLED.format(row="bucket")} >= 1 THEN 1 ELSE 0 END,
    updated_at = GREATEST(bucket.updated_at, CAST(:now AS timestamptz))
RETURNING
    (old.tokens IS NULL OR {_REFILLED.format(row="old")} >= 1) AS allowed,
    CAST(floor(new.tokens) AS bigint) AS remaining,
    CASE WHEN new.tokens >= 1 THEN 0
         ELSE CAST(ceil((1 - new.tokens) / CAST(:rate AS numeric)) AS bigint) END AS retry_after
"""  # noqa: S608 - built from constants only; every value is a bound parameter


@dataclass(frozen=True, slots=True)
class BucketPolicy:
    """``capacity`` tokens at most (the burst); ``refill_per_second`` tokens back per second."""

    capacity: int
    refill_per_second: Decimal

    def __post_init__(self) -> None:
        if self.capacity < 1 or self.refill_per_second <= 0:
            raise ValueError("a bucket needs capacity >= 1 and a positive refill rate")


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    limit: int
    remaining: int
    # Whole seconds until the next token (ceil); 0 when a token is available now.
    retry_after_seconds: int

    def headers(self) -> dict[str, str]:
        headers = {LIMIT_HEADER: str(self.limit), REMAINING_HEADER: str(self.remaining)}
        if not self.allowed:
            headers[RETRY_AFTER_HEADER] = str(max(1, self.retry_after_seconds))
        return headers


class RateLimiter:
    """Token buckets in ``rate_limit_buckets``; each ``take`` is its own short transaction."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def take(self, key: str, policy: BucketPolicy, now: datetime) -> Decision:
        async with self._session_factory() as session, session.begin():
            row = (
                await session.execute(
                    text(TAKE_TOKEN_SQL),
                    {
                        "key": key,
                        "capacity": policy.capacity,
                        "rate": policy.refill_per_second,
                        "now": now,
                    },
                )
            ).one()
        return Decision(
            allowed=bool(row.allowed),
            limit=policy.capacity,
            remaining=int(row.remaining),
            retry_after_seconds=int(row.retry_after),
        )


@dataclass(frozen=True, slots=True)
class RateLimitConfig:
    enabled: bool
    api_key: BucketPolicy
    operator: BucketPolicy
    auth_ip: BucketPolicy
    trusted_proxies: tuple[Network, ...] = ()

    @classmethod
    def from_settings(cls, settings: Settings) -> RateLimitConfig:
        return cls(
            enabled=settings.rate_limit_enabled,
            api_key=BucketPolicy(
                settings.rate_limit_api_key_capacity, settings.rate_limit_api_key_refill_per_second
            ),
            operator=BucketPolicy(
                settings.rate_limit_operator_capacity,
                settings.rate_limit_operator_refill_per_second,
            ),
            auth_ip=BucketPolicy(
                settings.rate_limit_auth_ip_capacity, settings.rate_limit_auth_ip_refill_per_second
            ),
            trusted_proxies=tuple(
                ipaddress.ip_network(entry, strict=False)
                for entry in settings.rate_limit_trusted_proxies
            ),
        )

    def principal_bucket(self, principal: Principal) -> tuple[str, BucketPolicy]:
        """API keys get the API key policy; every other principal (operators) the operator one."""
        policy = self.api_key if principal.kind == API_KEY_KIND else self.operator
        return f"{principal.kind}:{principal.subject_id}", policy


def _address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value.strip())
    except ValueError:
        return None


def _trusted(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address, proxies: Sequence[Network]
) -> bool:
    return any(address in network for network in proxies)


def client_ip(scope: Scope, trusted_proxies: Sequence[Network] = ()) -> str:
    """The client address; ``X-Forwarded-For`` counts only behind a trusted proxy."""
    client: Any = scope.get("client")
    peer = _address(str(client[0])) if client else None
    if peer is None:
        return UNKNOWN_IP
    if not trusted_proxies or not _trusted(peer, trusted_proxies):
        return str(peer)
    forwarded = Headers(scope=scope).get(FORWARDED_FOR_HEADER)
    if not forwarded:
        return str(peer)
    client_address = peer
    for hop in reversed(forwarded.split(",")):
        address = _address(hop)
        if address is None:
            break  # garbage from an untrusted hop: stop at the last address a proxy vouched for
        client_address = address
        if not _trusted(address, trusted_proxies):
            break
    return str(client_address)


def is_exempt(path: str) -> bool:
    return any(pattern.fullmatch(path) for pattern in EXEMPT_PATHS)


def rate_limited_response(request: Request, decision: Decision) -> Response:
    return problem_response(
        request,
        status=429,
        title=RateLimited.title,
        type_uri=problem_type(RateLimited.slug),
        detail=f"Too many requests. Retry after {max(1, decision.retry_after_seconds)} seconds.",
        headers=decision.headers(),
    )


def _limiter(state: Any) -> RateLimiter:
    return RateLimiter(state.session_factory)


async def _identify(request: Request) -> tuple[Principal | None, bool]:
    """Run the authenticator chain once: ``(principal, credentials_rejected)``.

    The principal is cached where ``current_principal`` looks for it, so the route does not
    authenticate again. A rejection is left for the route to answer (it re-runs the chain and
    raises the same ``Unauthorized``); other errors (CSRF...) are not authentication attempts.
    """
    authenticators: Sequence[Authenticator] = getattr(request.app.state, "authenticators", ())
    for authenticator in authenticators:
        try:
            principal = await authenticator.authenticate(request)
        except Unauthorized:
            return None, True
        except DomainError:
            return None, False
        if principal is not None:
            request.state.principal = principal
            return principal, False
    return None, False


class RateLimitMiddleware:
    """Per-principal buckets on every request, the client-IP bucket on rejected credentials."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or is_exempt(scope["path"]):
            await self.app(scope, receive, send)
            return
        state = scope["app"].state
        config: RateLimitConfig | None = getattr(state, "rate_limits", None)
        if config is None or not config.enabled:
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        principal, rejected = await _identify(request)
        if principal is not None:
            key, policy = config.principal_bucket(principal)
        elif rejected:
            key = f"{AUTH_IP_BUCKET}:{client_ip(scope, config.trusted_proxies)}"
            policy = config.auth_ip
        else:
            # auth_attempt() may still take a token further in: its headers are added here.
            await self.app(scope, receive, _with_headers(send, scope))
            return

        decision = await _limiter(state).take(key, policy, state.clock.now())
        if not decision.allowed:
            logger.warning("rate_limit.denied", bucket=key.split(":", 1)[0])
            await rate_limited_response(request, decision)(scope, receive, send)
            return
        _remember(scope, decision)
        await self.app(scope, receive, _with_headers(send, scope))


def _remember(scope: Scope, decision: Decision) -> None:
    scope.setdefault("state", {})[HEADERS_STATE] = decision.headers()


def _with_headers(send: Send, scope: Scope) -> Send:
    """Adds the headers of the latest allowed decision, even to error responses."""

    async def send_with_headers(message: Message) -> None:
        headers: Mapping[str, str] = scope.get("state", {}).get(HEADERS_STATE, {})
        if message["type"] == "http.response.start" and headers:
            response_headers = MutableHeaders(scope=message)
            for name, value in headers.items():
                response_headers.setdefault(name, value)
        await send(message)

    return send_with_headers


def auth_attempt() -> Any:
    """Route marker for unauthenticated credential checks (login): one token of the client-IP
    bucket per attempt, ``429`` once it is empty."""

    async def auth_attempt_limit(request: Request) -> None:
        state = request.app.state
        config: RateLimitConfig | None = getattr(state, "rate_limits", None)
        if config is None or not config.enabled:
            return
        key = f"{AUTH_IP_BUCKET}:{client_ip(request.scope, config.trusted_proxies)}"
        decision = await _limiter(state).take(key, config.auth_ip, state.clock.now())
        if not decision.allowed:
            logger.warning("rate_limit.denied", bucket=AUTH_IP_BUCKET)
            raise ShortCircuit(rate_limited_response(request, decision))
        _remember(request.scope, decision)

    return Depends(auth_attempt_limit)


AuthAttempt = Annotated[None, auth_attempt()]
