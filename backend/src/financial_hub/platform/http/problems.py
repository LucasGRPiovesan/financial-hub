"""RFC 9457 ``application/problem+json`` responses and the app's exception handlers (ADR-0001).

Responses never contain stack traces, exception messages of unexpected errors or request input
values; unexpected errors are logged server-side with the correlation id instead.
"""

from collections.abc import Mapping, Sequence
from http import HTTPStatus
from typing import Any, Final

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from financial_hub.shared.errors import (
    Conflict,
    DomainError,
    FieldError,
    Forbidden,
    IdempotencyConflict,
    IdempotencyKeyReused,
    InsufficientFunds,
    NotFound,
    RateLimited,
    Unauthorized,
    ValidationFailed,
)

PROBLEM_TYPE_BASE: Final = "https://financial-hub.dev/problems/"
PROBLEM_MEDIA_TYPE: Final = "application/problem+json"
REQUEST_LOCATIONS: Final = frozenset({"body", "query", "path", "header", "cookie"})

logger = structlog.get_logger()

# Most specific class first is not required: lookup walks the exception's MRO.
DOMAIN_STATUS: Final[Mapping[type[DomainError], int]] = {
    DomainError: 400,
    ValidationFailed: 422,
    Unauthorized: 401,
    Forbidden: 403,
    NotFound: 404,
    Conflict: 409,
    IdempotencyConflict: 409,
    IdempotencyKeyReused: 422,
    InsufficientFunds: 422,
    RateLimited: 429,
}

# Framework-raised HTTP errors (unknown route, wrong method...) reuse the domain slugs.
_HTTP_SLUGS: Final[Mapping[int, str]] = {
    401: Unauthorized.slug,
    403: Forbidden.slug,
    404: NotFound.slug,
    405: "method-not-allowed",
    409: Conflict.slug,
    422: ValidationFailed.slug,
    429: RateLimited.slug,
}


class ProblemFieldError(BaseModel):
    pointer: str
    detail: str
    code: str | None = None
    location: str | None = None


class Problem(BaseModel):
    """Schema of every error response (for OpenAPI ``responses=``)."""

    type: str
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None
    correlation_id: str | None = None
    errors: list[ProblemFieldError] | None = None


def problem_type(slug: str) -> str:
    return f"{PROBLEM_TYPE_BASE}{slug}"


def json_pointer(parts: Sequence[str | int]) -> str:
    """RFC 6901 pointer (``~`` -> ``~0``, ``/`` -> ``~1``); ``""`` is the whole document."""
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in parts)


def _correlation_id(request: Request) -> str | None:
    value = getattr(request.state, "correlation_id", None)
    return value if isinstance(value, str) else None


def problem_response(
    request: Request,
    *,
    status: int,
    title: str,
    type_uri: str,
    detail: str | None = None,
    errors: Sequence[ProblemFieldError] = (),
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    problem = Problem(
        type=type_uri,
        title=title,
        status=status,
        detail=detail,
        instance=request.url.path,
        correlation_id=_correlation_id(request),
        errors=list(errors) or None,
    )
    return JSONResponse(
        problem.model_dump(exclude_none=True),
        status_code=status,
        headers=dict(headers) if headers else None,
        media_type=PROBLEM_MEDIA_TYPE,
    )


def status_for(error: DomainError) -> int:
    for cls in type(error).__mro__:
        status = DOMAIN_STATUS.get(cls)  # pyright: ignore[reportArgumentType]
        if status is not None:
            return status
    return 400  # pragma: no cover - DomainError itself is mapped


def _field_error(error: FieldError) -> ProblemFieldError:
    return ProblemFieldError(pointer=error.pointer, detail=error.detail, code=error.code)


async def domain_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, DomainError):
        raise exc
    headers: dict[str, str] = {}
    if isinstance(exc, RateLimited) and exc.retry_after_seconds is not None:
        headers["Retry-After"] = str(exc.retry_after_seconds)
    return problem_response(
        request,
        status=status_for(exc),
        title=exc.title,
        type_uri=problem_type(exc.slug),
        detail=exc.detail,
        errors=[_field_error(error) for error in exc.errors],
        headers=headers,
    )


def validation_field_errors(errors: Sequence[Any]) -> list[ProblemFieldError]:
    """Map pydantic/FastAPI ``loc`` tuples to pointers (relative to the body or parameter set)."""
    result: list[ProblemFieldError] = []
    for error in errors:
        loc: tuple[str | int, ...] = tuple(error.get("loc", ()))
        location = str(loc[0]) if loc and loc[0] in REQUEST_LOCATIONS else None
        path = loc[1:] if location is not None else loc
        # Only pydantic's message is exposed, never error["input"] (it may carry secrets).
        result.append(
            ProblemFieldError(
                pointer=json_pointer(path),
                detail=str(error.get("msg", "invalid value")),
                code=error.get("type"),
                location=location,
            )
        )
    return result


async def request_validation_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):
        raise exc
    return problem_response(
        request,
        status=422,
        title=ValidationFailed.title,
        type_uri=problem_type(ValidationFailed.slug),
        detail="The request contains invalid fields.",
        errors=validation_field_errors(exc.errors()),
    )


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, StarletteHTTPException):
        raise exc
    status = exc.status_code
    try:
        phrase = HTTPStatus(status).phrase
    except ValueError:
        phrase = "Error"
    slug = _HTTP_SLUGS.get(status)
    # RFC 9457 4.2.1: "about:blank" means the problem is just the HTTP status.
    type_uri = problem_type(slug) if slug else "about:blank"
    detail = exc.detail if isinstance(exc.detail, str) and status < 500 else None  # pyright: ignore[reportUnnecessaryIsInstance]
    return problem_response(
        request,
        status=status,
        title=phrase,
        type_uri=type_uri,
        detail=detail if detail != phrase else None,
        headers=exc.headers,
    )


def internal_error_response(request: Request) -> JSONResponse:
    return problem_response(
        request,
        status=500,
        title="Internal server error",
        type_uri=problem_type("internal-error"),
        detail="An unexpected error occurred. Quote the correlation id when reporting it.",
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # Last resort (ServerErrorMiddleware); CorrelationIdMiddleware normally answers first.
    logger.error("http.unhandled_exception", exc_info=exc)
    return internal_error_response(request)


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(DomainError, domain_error_handler)
    app.add_exception_handler(RequestValidationError, request_validation_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
