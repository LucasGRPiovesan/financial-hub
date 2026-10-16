"""Request protection: ``Idempotency-Key`` replay (ADR-0004) and token-bucket rate limits.

``configure_protection`` must run before ``CorrelationIdMiddleware`` is added, so both
middlewares sit inside it (their 409/429 problems carry the correlation id) with rate limiting
outside idempotency (a limited request never claims a key).
"""

from fastapi import FastAPI

from financial_hub.platform.http.protection.idempotency import (
    IdempotencyConfig,
    IdempotencyMiddleware,
)
from financial_hub.platform.http.protection.rate_limit import RateLimitConfig, RateLimitMiddleware
from financial_hub.platform.http.protection.short_circuit import (
    ShortCircuit,
    short_circuit_handler,
)
from financial_hub.platform.settings import Settings


def configure_protection(app: FastAPI, settings: Settings) -> None:
    app.state.idempotency = IdempotencyConfig.from_settings(settings)
    app.state.rate_limits = RateLimitConfig.from_settings(settings)
    app.add_exception_handler(ShortCircuit, short_circuit_handler)
    app.add_middleware(IdempotencyMiddleware)  # innermost of the two
    app.add_middleware(RateLimitMiddleware)
