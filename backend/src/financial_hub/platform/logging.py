"""structlog JSON logging with request context, trace correlation and secret/PII redaction.

Rendered events go through the stdlib root logger, so the OTLP log handler installed by
``platform.telemetry`` exports exactly what is written to stdout (already redacted).
"""

import logging
import re
import sys
from collections.abc import Generator, Mapping, MutableMapping
from contextlib import contextmanager
from typing import Any, Final

import structlog
from opentelemetry import trace
from structlog.typing import EventDict, WrappedLogger

REDACTED: Final = "[REDACTED]"
SENSITIVE_KEY: Final = re.compile(
    r"password|secret|token|authorization|access_token|api[_-]?key|cpf|cnpj", re.IGNORECASE
)

_handler: logging.Handler | None = None


def _redact_mapping(mapping: Mapping[Any, Any]) -> dict[Any, Any]:
    return {
        key: REDACTED if isinstance(key, str) and SENSITIVE_KEY.search(key) else _redact(item)
        for key, item in mapping.items()
    }


def _redact(value: object) -> object:
    if isinstance(value, Mapping):
        return _redact_mapping(value)  # pyright: ignore[reportUnknownArgumentType]
    if isinstance(value, list | tuple | set | frozenset):
        return [_redact(item) for item in value]  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]
    return value


def redact_sensitive(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """Mask values whose key looks sensitive, at any nesting depth (dicts, lists, tuples).

    Matching is by key name only (case-insensitive substring); values are never inspected, so
    secrets must not be interpolated into free-text messages. Input objects are not mutated.
    """
    return _redact_mapping(event_dict)


def add_trace_context(
    _logger: WrappedLogger, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    span_context = trace.get_current_span().get_span_context()
    if span_context.is_valid:
        event_dict.setdefault("trace_id", format(span_context.trace_id, "032x"))
        event_dict.setdefault("span_id", format(span_context.span_id, "016x"))
    return event_dict


def configure_logging(level: str = "INFO") -> None:
    """Idempotent: safe to call from every entrypoint (api, worker, tests)."""
    global _handler
    numeric_level = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)

    root = logging.getLogger()
    if _handler is not None:
        root.removeHandler(_handler)
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(_handler)
    root.setLevel(numeric_level)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            add_trace_context,
            structlog.processors.format_exc_info,
            redact_sensitive,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        cache_logger_on_first_use=True,
    )


def bind_request_context(*, correlation_id: str, request_id: str | None = None) -> None:
    values = {"correlation_id": correlation_id}
    if request_id is not None:
        values["request_id"] = request_id
    structlog.contextvars.bind_contextvars(**values)


def bind_project_id(project_id: str) -> None:
    """Called by authentication once the request's project is known."""
    structlog.contextvars.bind_contextvars(project_id=project_id)


@contextmanager
def log_context(**values: str) -> Generator[None]:
    """Bind context (``correlation_id``, ``request_id``, ``project_id``...) for a block only."""
    with structlog.contextvars.bound_contextvars(**values):
        yield
