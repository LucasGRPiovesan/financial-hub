"""Correlation id + request logging middleware (pure ASGI: contextvars flow into handlers)."""

import re
import time
import uuid
from typing import Final

import structlog
from opentelemetry import trace
from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from financial_hub.platform.http.problems import internal_error_response

CORRELATION_HEADER: Final = "X-Correlation-Id"
# Accept caller ids that are safe to log and echo (no spaces/control chars, bounded length).
VALID_CORRELATION_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
QUIET_PATH_PREFIX: Final = "/health/"

logger = structlog.get_logger()


def new_correlation_id() -> str:
    return str(uuid.uuid7())


def resolve_correlation_id(value: str | None) -> str:
    """Keep a valid incoming id, otherwise generate one."""
    if value is not None and VALID_CORRELATION_ID.fullmatch(value):
        return value
    return new_correlation_id()


class CorrelationIdMiddleware:
    """Binds ``correlation_id``/``request_id`` to logs, the current span and ``request.state``.

    Echoes ``X-Correlation-Id`` on every response, logs one ``http.request`` line per request
    (method, path, status, duration; never bodies or query strings) and turns unhandled errors
    into a problem+json 500 that still carries the correlation id.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        correlation_id = resolve_correlation_id(Headers(scope=scope).get(CORRELATION_HEADER))
        request_id = str(uuid.uuid7())
        state = scope.setdefault("state", {})
        state["correlation_id"] = correlation_id
        state["request_id"] = request_id
        span = trace.get_current_span()
        span.set_attribute("correlation_id", correlation_id)
        span.set_attribute("request_id", request_id)

        status_code = 500
        response_started = False

        async def send_with_header(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                MutableHeaders(scope=message).append(CORRELATION_HEADER, correlation_id)
            await send(message)

        started_at = time.perf_counter()
        with structlog.contextvars.bound_contextvars(
            correlation_id=correlation_id, request_id=request_id
        ):
            try:
                await self.app(scope, receive, send_with_header)
            except Exception as exc:
                logger.exception("http.unhandled_exception")
                span.record_exception(exc)
                span.set_status(trace.StatusCode.ERROR)
                if response_started:
                    raise
                response = internal_error_response(Request(scope))
                await response(scope, receive, send_with_header)
            finally:
                path: str = scope["path"]
                log = logger.debug if path.startswith(QUIET_PATH_PREFIX) else logger.info
                log(
                    "http.request",
                    method=scope["method"],
                    path=path,
                    status=status_code,
                    duration_ms=round((time.perf_counter() - started_at) * 1000, 3),
                )
