"""``GET /v1/projects/{project_id}/stream``: the project's live events as Server-Sent Events.

ADR-0010 names the endpoint ``/v1/stream``; the stream is project-scoped like every other route
(``require()`` needs ``{project_id}`` in the path, ADR-0011), so the project is in the path.

Frames: ``id:`` is an opaque cursor (send it back as ``Last-Event-ID`` or ``?last_event_id=`` to
resume), ``event:`` the event type, ``data:`` the JSON envelope. ``event: resync`` (no ``id``)
means "refetch your state"; ``: ping`` comments keep idle connections open through proxies.
"""

import json
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse

from financial_hub.modules.events.application.realtime import RealtimeBroker, StreamMessage
from financial_hub.modules.identity.application.api import Permission
from financial_hub.platform.http.auth import ProjectScope, require
from financial_hub.shared.ids import IdPrefix, format_id

router = APIRouter(prefix="/v1/projects/{project_id}", tags=["events"])

RETRY_MS = 3000
_PING = b": ping\n\n"
_RESYNC = b"event: resync\ndata: {}\n\n"


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def encode(message: StreamMessage) -> bytes:
    """One SSE frame for ``message``."""
    event = message.event
    if event is None:
        return _PING if message.kind == "heartbeat" else _RESYNC
    envelope = {
        "id": event.public_id,
        "type": event.type,
        "version": event.version,
        "project_id": format_id(IdPrefix.PROJECT, event.project_id),
        "subject": event.subject,
        "occurred_at": event.occurred_at.isoformat(),
        "correlation_id": None if event.correlation_id is None else str(event.correlation_id),
        "causation_id": (
            None if event.causation_id is None else format_id(IdPrefix.EVENT, event.causation_id)
        ),
        "data": event.data,
    }
    return f"id: {event.cursor.encode()}\nevent: {event.type}\ndata: {_json(envelope)}\n\n".encode()


async def _frames(messages: AsyncGenerator[StreamMessage]) -> AsyncIterator[bytes]:
    yield f"retry: {RETRY_MS}\n\n".encode()
    try:
        async for message in messages:
            yield encode(message)
    finally:
        await messages.aclose()


@router.get("/stream", response_class=StreamingResponse)
async def stream(
    request: Request,
    scope: Annotated[ProjectScope, require(Permission.EVENTS_READ)],
    header_last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    query_last_event_id: Annotated[str | None, Query(alias="last_event_id")] = None,
) -> StreamingResponse:
    broker: RealtimeBroker = request.app.state.realtime
    messages = broker.stream(scope.project_id, header_last_event_id or query_last_event_id)
    return StreamingResponse(
        _frames(messages),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
