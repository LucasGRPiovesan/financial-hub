"""``Idempotency-Key`` on creating POST routes (ADR-0004).

Routes opt in with the ``idempotent()`` dependency, placed **after** the route's access
declaration so the caller is authenticated and authorised first::

    async def create(body: Body, scope: Manage, _: Annotated[None, idempotent()]) -> Out: ...

``mode="required"`` answers ``400`` when the header is missing (financial-object creators);
``"optional"`` processes keyless requests normally. A key is 1..255 visible ASCII characters,
scoped to ``(project, principal)``: the ``{project_id}`` of the path (nil UUID on global routes)
and the caller's public id, so two API keys or operators never share a key space.

Claim protocol:

1. Before the handler runs, ``INSERT ... ON CONFLICT DO UPDATE ... WHERE expired`` claims the key
   in its own short committed transaction (``in_progress``). A live row is not touched: the
   request is answered from it instead (same key + different method/path/body hash ``422``;
   still in progress ``409`` + ``Retry-After: 1``; completed: the stored status, body and
   allowlisted headers, plus ``Idempotent-Replayed: true``). Expired rows are overwritten.
2. ``IdempotencyMiddleware`` buffers the claimed response, stores it (``completed``) and only
   then sends it. 4xx answers are stored and replayed; a 5xx or an exception deletes the claim
   so the client may retry.

Bodies above ``FH_IDEMPOTENCY_MAX_BODY_BYTES``, and success bodies of routes declared
``store_success_body=False`` (a secret shown once), are not stored: the record keeps the status
only and a replay answers ``409`` (``.../idempotency-response-unavailable``) naming the original
status, with ``Idempotent-Replayed: true``.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Any, Final, Literal
from uuid import UUID

import structlog
from fastapi import Depends, Header, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from financial_hub.platform.http.auth import PROJECT_PATH_PARAM, current_principal
from financial_hub.platform.http.problems import problem_response, problem_type
from financial_hub.platform.http.protection.short_circuit import ShortCircuit
from financial_hub.platform.settings import Settings
from financial_hub.shared.errors import DomainError, IdempotencyConflict, IdempotencyKeyReused
from financial_hub.shared.ids import IdPrefix, InvalidIdError, parse_id

IDEMPOTENCY_HEADER: Final = "Idempotency-Key"
REPLAYED_HEADER: Final = "Idempotent-Replayed"
VALID_KEY: Final = re.compile(r"[\x21-\x7e]{1,255}")
NIL_PROJECT: Final = UUID(int=0)
STORED_HEADERS: Final = frozenset({"content-type", "location"})
CLAIM_STATE: Final = "idempotency_claim"
IN_PROGRESS_RETRY_AFTER: Final = "1"
UNAVAILABLE_SLUG: Final = "idempotency-response-unavailable"

type IdempotencyMode = Literal["optional", "required"]

logger = structlog.get_logger()


class IdempotencyKeyRequired(DomainError):
    slug = "idempotency-key-required"
    title = "Idempotency-Key header required"


class IdempotencyKeyInvalid(DomainError):
    slug = "idempotency-key-invalid"
    title = "Invalid Idempotency-Key header"


@dataclass(frozen=True, slots=True)
class IdempotencyConfig:
    ttl: timedelta
    max_body_bytes: int

    @classmethod
    def from_settings(cls, settings: Settings) -> IdempotencyConfig:
        return cls(
            ttl=timedelta(hours=settings.idempotency_ttl_hours),
            max_body_bytes=settings.idempotency_max_body_bytes,
        )


@dataclass(frozen=True, slots=True)
class Claim:
    """A key this request owns (``in_progress``); ``claimed_at`` fences the owner's writes."""

    project_id: UUID
    principal_id: str
    key: str
    claimed_at: datetime
    store_success_body: bool = True


@dataclass(frozen=True, slots=True)
class StoredResponse:
    status: int
    headers: dict[str, str]
    body: bytes | None


@dataclass(frozen=True, slots=True)
class ExistingRecord:
    request_hash: bytes
    completed: bool
    response: StoredResponse | None


def request_hash(method: str, path: str, body: bytes) -> bytes:
    """SHA-256 over the method, the path and the SHA-256 of the raw body."""
    digest = hashlib.sha256(body).hexdigest()
    return hashlib.sha256(f"{method.upper()}\n{path}\n{digest}".encode()).digest()


_CLAIM_SQL: Final = text("""
INSERT INTO idempotency_records AS record
    (project_id, principal_id, key, method, path, request_hash, status, created_at, expires_at)
VALUES
    (:project_id, :principal_id, :key, :method, :path, :request_hash, 'in_progress',
     :now, :expires_at)
ON CONFLICT (project_id, principal_id, key) DO UPDATE SET
    method = excluded.method,
    path = excluded.path,
    request_hash = excluded.request_hash,
    status = 'in_progress',
    response_status = NULL,
    response_headers = NULL,
    response_body = NULL,
    created_at = excluded.created_at,
    expires_at = excluded.expires_at
WHERE record.expires_at <= excluded.created_at
RETURNING true AS claimed
""")
_EXISTING_SQL: Final = text("""
SELECT request_hash, status, response_status, response_headers, response_body
FROM idempotency_records
WHERE project_id = :project_id AND principal_id = :principal_id AND key = :key
""")
_COMPLETE_SQL: Final = text("""
UPDATE idempotency_records
SET status = 'completed', response_status = :status,
    response_headers = CAST(:headers AS jsonb), response_body = :body
WHERE project_id = :project_id AND principal_id = :principal_id AND key = :key
  AND status = 'in_progress' AND created_at = :claimed_at
""")
_RELEASE_SQL: Final = text("""
DELETE FROM idempotency_records
WHERE project_id = :project_id AND principal_id = :principal_id AND key = :key
  AND status = 'in_progress' AND created_at = :claimed_at
""")
_PURGE_SQL: Final = text("DELETE FROM idempotency_records WHERE expires_at <= :now")
# A claim that vanished between the conflict and the read (released after a 5xx) is retried.
_CLAIM_ATTEMPTS: Final = 3


def _identity(claim: Claim) -> dict[str, Any]:
    return {"project_id": claim.project_id, "principal_id": claim.principal_id, "key": claim.key}


class IdempotencyStore:
    """``idempotency_records``; every call is its own short transaction."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def claim(
        self, claim: Claim, *, method: str, path: str, request_hash: bytes, expires_at: datetime
    ) -> ExistingRecord | None:
        """``None`` when ``claim`` now owns the key, otherwise the live record that holds it."""
        params = {
            **_identity(claim),
            "method": method,
            "path": path,
            "request_hash": request_hash,
            "now": claim.claimed_at,
            "expires_at": expires_at,
        }
        for _ in range(_CLAIM_ATTEMPTS):
            async with self._session_factory() as session, session.begin():
                if (await session.execute(_CLAIM_SQL, params)).first() is not None:
                    return None
                row = (await session.execute(_EXISTING_SQL, _identity(claim))).first()
            if row is not None:
                return _existing(row)
        raise RuntimeError("idempotency key could not be claimed nor read")  # pragma: no cover

    async def complete(self, claim: Claim, response: StoredResponse) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                _COMPLETE_SQL,
                {
                    **_identity(claim),
                    "claimed_at": claim.claimed_at,
                    "status": response.status,
                    "headers": json.dumps(response.headers),
                    "body": response.body,
                },
            )

    async def release(self, claim: Claim) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                _RELEASE_SQL, {**_identity(claim), "claimed_at": claim.claimed_at}
            )

    async def purge_expired(self, now: datetime) -> int:
        async with self._session_factory() as session, session.begin():
            result = await session.execute(_PURGE_SQL, {"now": now})
        return int(getattr(result, "rowcount", 0) or 0)


def _existing(row: Any) -> ExistingRecord:
    completed = row.status == "completed"
    response = None
    if completed:
        stored_headers: Any = row.response_headers or {}
        response = StoredResponse(
            status=int(row.response_status),
            headers={str(name): str(value) for name, value in dict(stored_headers).items()},
            body=None if row.response_body is None else bytes(row.response_body),
        )
    return ExistingRecord(
        request_hash=bytes(row.request_hash), completed=completed, response=response
    )


def _answer(request: Request, record: ExistingRecord, digest: bytes) -> Response:
    """The response for a request whose key is held by ``record``."""
    if record.request_hash != digest:
        return problem_response(
            request,
            status=422,
            title=IdempotencyKeyReused.title,
            type_uri=problem_type(IdempotencyKeyReused.slug),
            detail="This Idempotency-Key was used with a different request.",
        )
    stored = record.response
    if stored is None:
        return problem_response(
            request,
            status=409,
            title=IdempotencyConflict.title,
            type_uri=problem_type(IdempotencyConflict.slug),
            detail="The original request is still being processed. Retry shortly.",
            headers={"Retry-After": IN_PROGRESS_RETRY_AFTER},
        )
    if stored.body is None:
        return problem_response(
            request,
            status=409,
            title="Idempotent response not available",
            type_uri=problem_type(UNAVAILABLE_SLUG),
            detail=(
                f"The original request completed with status {stored.status}; its response "
                "is not stored and cannot be replayed."
            ),
            headers={REPLAYED_HEADER: "true"},
        )
    return Response(
        content=stored.body,
        status_code=stored.status,
        headers={**stored.headers, REPLAYED_HEADER: "true"},
    )


async def _scope_project(request: Request) -> UUID | None:
    """The project the key lives in; ``None`` to skip idempotency (the access check answers)."""
    principal = await current_principal(request)
    raw = request.path_params.get(PROJECT_PATH_PARAM)
    if raw is None:
        return NIL_PROJECT
    try:
        project_id = parse_id(str(raw), IdPrefix.PROJECT)
    except InvalidIdError:
        return None
    return project_id if principal.scope_for(project_id) is not None else None


def idempotent(mode: IdempotencyMode = "optional", *, store_success_body: bool = True) -> Any:
    """Route dependency: honour ``Idempotency-Key`` (see the module docstring)."""

    async def idempotency_key(
        request: Request,
        key: Annotated[
            str | None,
            Header(
                alias=IDEMPOTENCY_HEADER,
                description="1..255 visible ASCII characters; retries with the same key and "
                "body replay the first response for 24 hours (ADR-0004).",
            ),
        ] = None,
    ) -> None:
        if key is None:
            if mode == "required":
                raise IdempotencyKeyRequired("This operation requires an Idempotency-Key header.")
            return
        if not VALID_KEY.fullmatch(key):
            raise IdempotencyKeyInvalid(
                "Idempotency-Key must be 1 to 255 visible ASCII characters."
            )
        project_id = await _scope_project(request)
        if project_id is None:
            return
        state = request.app.state
        config: IdempotencyConfig = state.idempotency
        principal = await current_principal(request)
        claim = Claim(
            project_id=project_id,
            principal_id=principal.subject_id,
            key=key,
            claimed_at=state.clock.now(),
            store_success_body=store_success_body,
        )
        digest = request_hash(request.method, request.url.path, await request.body())
        existing = await IdempotencyStore(state.session_factory).claim(
            claim,
            method=request.method,
            path=request.url.path,
            request_hash=digest,
            expires_at=claim.claimed_at + config.ttl,
        )
        if existing is not None:
            raise ShortCircuit(_answer(request, existing, digest))
        setattr(request.state, CLAIM_STATE, claim)

    return Depends(idempotency_key)


def _stored_headers(start: Message) -> dict[str, str]:
    headers: dict[str, str] = {}
    for raw_name, raw_value in start.get("headers", ()):
        name = bytes(raw_name).decode("latin-1").lower()
        if name in STORED_HEADERS:
            headers[name] = bytes(raw_value).decode("latin-1")
    return headers


class IdempotencyMiddleware:
    """Stores the response of a request that claimed a key before sending it (pure ASGI).

    Requests without a claim stream through untouched. Must sit inside the exception handling
    (``ExceptionMiddleware`` renders 4xx before this sees them; unhandled errors propagate here
    and release the claim).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        state: dict[str, Any] = scope.setdefault("state", {})
        start: Message | None = None
        chunks: list[bytes] = []
        settled = False

        async def capture(message: Message) -> None:
            nonlocal start, settled
            claim: Claim | None = state.get(CLAIM_STATE)
            if claim is None or settled:
                await send(message)
                return
            if message["type"] == "http.response.start":
                start = message
                return
            if message["type"] != "http.response.body" or start is None:
                await send(message)  # pragma: no cover - ASGI ordering guarantees start first
                return
            chunks.append(bytes(message.get("body", b"")))
            if message.get("more_body", False):
                return
            settled = True
            body = b"".join(chunks)
            await _settle(scope, claim, start, body)
            await send(start)
            await send({"type": "http.response.body", "body": body, "more_body": False})

        try:
            await self.app(scope, receive, capture)
        except BaseException:
            claim: Claim | None = state.get(CLAIM_STATE)
            if claim is not None and not settled:
                await _release(scope, claim)
            raise


def _store(scope: Scope) -> tuple[IdempotencyStore, IdempotencyConfig]:
    app_state = scope["app"].state
    return IdempotencyStore(app_state.session_factory), app_state.idempotency


async def _release(scope: Scope, claim: Claim) -> None:
    store, _ = _store(scope)
    try:
        await store.release(claim)
    except Exception:
        logger.exception("idempotency.release_failed")


async def _settle(scope: Scope, claim: Claim, start: Message, body: bytes) -> None:
    """5xx: release the key. Otherwise record the response (``completed``)."""
    status = int(start["status"])
    if status >= 500:
        await _release(scope, claim)
        return
    store, config = _store(scope)
    stored_body: bytes | None = body
    if status < 400 and not claim.store_success_body:
        stored_body = None
    elif len(body) > config.max_body_bytes:
        logger.warning("idempotency.body_not_stored", size=len(body), limit=config.max_body_bytes)
        stored_body = None
    try:
        await store.complete(
            claim, StoredResponse(status=status, headers=_stored_headers(start), body=stored_body)
        )
    except Exception:
        # The effect happened: keep the claim (in_progress, 409 until it expires) rather than let
        # a retry run the operation twice.
        logger.exception("idempotency.store_failed")
