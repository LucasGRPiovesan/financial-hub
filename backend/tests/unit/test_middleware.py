import json
import uuid
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
import structlog
from fastapi import FastAPI, Request
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from starlette.types import Message, Receive, Scope, Send

from financial_hub.main import create_app
from financial_hub.platform.http.middleware import (
    CORRELATION_HEADER,
    CorrelationIdMiddleware,
    resolve_correlation_id,
)


@pytest.fixture(autouse=True)
def clean_context() -> Iterator[None]:
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()


def build_app() -> FastAPI:
    app = create_app()

    @app.get("/context")
    async def context(request: Request) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        return {
            "state": request.state.correlation_id,
            "request_id": request.state.request_id,
            "logging": structlog.contextvars.get_contextvars(),
        }

    @app.get("/boom")
    async def boom() -> None:  # pyright: ignore[reportUnusedFunction]
        raise RuntimeError("kaboom")

    return app


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=build_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.parametrize("value", ["test-123", "a", "01J9ABC.def:ghi_jkl-0", "x" * 128])
def test_valid_incoming_ids_are_kept(value: str) -> None:
    assert resolve_correlation_id(value) == value


@pytest.mark.parametrize(
    "value", [None, "", " test", "has space", "-leading-dash", "x" * 129, "bad\nnewline", "ç"]
)
def test_invalid_incoming_ids_are_replaced_by_uuid7(value: str | None) -> None:
    assert uuid.UUID(resolve_correlation_id(value)).version == 7


async def test_echoes_valid_correlation_id(client: httpx.AsyncClient) -> None:
    response = await client.get("/health/live", headers={CORRELATION_HEADER: "test-123"})

    assert response.status_code == 200
    assert response.headers[CORRELATION_HEADER] == "test-123"


async def test_generates_correlation_id_when_missing_or_invalid(
    client: httpx.AsyncClient,
) -> None:
    missing = await client.get("/health/live")
    invalid = await client.get("/health/live", headers={CORRELATION_HEADER: "bad id"})

    for response in (missing, invalid):
        generated = response.headers[CORRELATION_HEADER]
        assert uuid.UUID(generated).version == 7
    assert missing.headers[CORRELATION_HEADER] != invalid.headers[CORRELATION_HEADER]


async def test_ids_reach_request_state_and_log_context(client: httpx.AsyncClient) -> None:
    response = await client.get("/context", headers={CORRELATION_HEADER: "corr-9"})

    body = response.json()
    assert body["state"] == "corr-9"
    assert body["logging"] == {"correlation_id": "corr-9", "request_id": body["request_id"]}
    assert uuid.UUID(body["request_id"]).version == 7
    assert structlog.contextvars.get_contextvars() == {}


async def test_logs_one_request_line_without_body_or_query(
    caplog: pytest.LogCaptureFixture, client: httpx.AsyncClient
) -> None:
    await client.get("/context?token=abc", headers={CORRELATION_HEADER: "corr-log"})

    lines = [json.loads(message) for message in caplog.messages if message.startswith("{")]
    requests = [line for line in lines if line["event"] == "http.request"]
    assert len(requests) == 1
    entry = requests[0]
    assert entry["method"] == "GET"
    assert entry["path"] == "/context"
    assert entry["status"] == 200
    assert entry["correlation_id"] == "corr-log"
    assert isinstance(entry["duration_ms"], float)
    assert "abc" not in json.dumps(entry)


async def test_unhandled_error_keeps_correlation_id_and_logs_500(
    caplog: pytest.LogCaptureFixture, client: httpx.AsyncClient
) -> None:
    response = await client.get("/boom", headers={CORRELATION_HEADER: "corr-500"})

    assert response.status_code == 500
    assert response.headers[CORRELATION_HEADER] == "corr-500"
    assert response.json()["correlation_id"] == "corr-500"
    lines = [json.loads(message) for message in caplog.messages if message.startswith("{")]
    events = {line["event"]: line for line in lines}
    assert events["http.unhandled_exception"]["correlation_id"] == "corr-500"
    assert "kaboom" in events["http.unhandled_exception"]["exception"]
    assert events["http.request"]["status"] == 500


async def test_span_carries_correlation_id() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    app = build_app()
    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        ok = await client.get("/health/live", headers={CORRELATION_HEADER: "test-123"})
        failed = await client.get("/boom", headers={CORRELATION_HEADER: "test-500"})

    assert ok.headers[CORRELATION_HEADER] == "test-123"
    assert failed.status_code == 500
    servers = {
        span.attributes["correlation_id"]: span
        for span in exporter.get_finished_spans()
        if span.attributes and "correlation_id" in span.attributes
    }
    assert set(servers) == {"test-123", "test-500"}
    assert servers["test-500"].status.status_code is StatusCode.ERROR


async def test_non_http_scopes_pass_through() -> None:
    seen: list[str] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    async def receive() -> Message:
        return {"type": "lifespan.startup"}

    async def send(message: Message) -> None:
        return None

    await CorrelationIdMiddleware(inner)({"type": "lifespan"}, receive, send)

    assert seen == ["lifespan"]


async def test_error_after_response_started_is_reraised() -> None:
    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError("mid-stream")

    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b""}

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {"type": "http", "method": "GET", "path": "/stream", "headers": []}
    with pytest.raises(RuntimeError, match="mid-stream"):
        await CorrelationIdMiddleware(inner)(scope, receive, send)

    assert len(sent) == 1
    header_names = [name for name, _ in sent[0]["headers"]]
    assert b"x-correlation-id" in header_names
