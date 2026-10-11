"""Helpers for the SSE stream tests: a broker on real Postgres (as ``fh_app``) driven by hand.

Most tests do not run the broker loop: they publish, call ``broker.pump()`` (what a wake-up does)
and read the stream, so nothing depends on timing. The loop, ``LISTEN`` and polling have their own
tests. Heartbeats are 50 ms in tests, and ``events_only`` skips them.
"""

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from uuid import UUID

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from financial_hub.modules.events.application import Event
from financial_hub.modules.events.application.realtime import (
    RealtimeBroker,
    RealtimeConfig,
    StreamMessage,
)
from financial_hub.platform.db import create_session_factory
from financial_hub.shared.clock import FrozenClock
from financial_hub.shared.ids import new_uuid
from tests.integration.events.support import AT, Harness, make_app_engine

FAST = RealtimeConfig(heartbeat_seconds=0.05, poll_interval_seconds=0.05)


class ManualWaker:
    """A waker nobody ever wakes: ``wait`` just times out (the test pumps the broker itself)."""

    async def wait(self, seconds: float) -> bool:
        await asyncio.sleep(min(seconds, 0.01))
        return False

    async def close(self) -> None:
        return None


@dataclass
class Rig:
    app_engine: AsyncEngine
    owner_engine: AsyncEngine
    factory: async_sessionmaker[AsyncSession]
    clock: FrozenClock = field(default_factory=lambda: FrozenClock(AT + timedelta(hours=1)))

    @classmethod
    @asynccontextmanager
    async def open(cls, owner_engine: AsyncEngine) -> AsyncIterator[Rig]:
        engine = make_app_engine()
        try:
            yield cls(engine, owner_engine, create_session_factory(engine))
        finally:
            await engine.dispose()

    def project(self) -> Harness:
        """A fresh project's publishing harness."""
        return Harness(self.factory, self.owner_engine, new_uuid())

    async def broker(self, config: RealtimeConfig = FAST) -> RealtimeBroker:
        broker = RealtimeBroker(self.factory, ManualWaker(), config=config, clock=self.clock)
        assert await broker.anchor()
        return broker


async def pump_to(broker: RealtimeBroker, *events: Event) -> None:
    """Pump until the broker has read past ``events`` (the visibility horizon may lag a bit)."""
    target = max(event.cursor for event in events)
    async with asyncio.timeout(10):
        while broker.cursor is None or broker.cursor < target:
            await broker.pump()
            await asyncio.sleep(0.01)


async def events_only(
    stream: AsyncGenerator[StreamMessage], count: int, *, seconds: float = 5
) -> list[StreamMessage]:
    """The next ``count`` messages that are not heartbeats."""
    found: list[StreamMessage] = []
    async with asyncio.timeout(seconds):
        while len(found) < count:
            message = await anext(stream)
            if message.kind != "heartbeat":
                found.append(message)
    return found


def ids(messages: list[StreamMessage]) -> list[UUID]:
    return [m.event.id for m in messages if m.event is not None]


def cursor_of(event: Event) -> str:
    return event.cursor.encode()


async def prime(stream: AsyncGenerator[StreamMessage]) -> None:
    """Start a live-only stream: its first message is a heartbeat, so it is subscribed."""
    first = await anext(stream)
    assert first.kind == "heartbeat"


async def publish_many(project: Harness, count: int, **overrides: Any) -> list[Event]:
    return [await project.publish(f"m{i}", **overrides) for i in range(count)]


# --- HTTP: a minimal streaming ASGI client (httpx's ASGITransport buffers whole responses) ------


@dataclass(frozen=True, slots=True)
class Frame:
    raw: str

    @property
    def is_comment(self) -> bool:
        return self.raw.startswith(":")

    def field(self, name: str) -> str | None:
        prefix = f"{name}: "
        return next(
            (line[len(prefix) :] for line in self.raw.split("\n") if line.startswith(prefix)), None
        )

    @property
    def data(self) -> Any:
        raw = self.field("data")
        return None if raw is None else json.loads(raw)


@dataclass
class SseResponse:
    status: int = 0
    headers: dict[str, str] = field(default_factory=lambda: {})
    body: bytes = b""
    frames: asyncio.Queue[Frame] = field(default_factory=lambda: asyncio.Queue())
    _buffer: str = ""

    async def frame(self, seconds: float = 5) -> Frame:
        async with asyncio.timeout(seconds):
            return await self.frames.get()

    async def event_frames(self, count: int, seconds: float = 5) -> list[Frame]:
        """The next ``count`` frames that carry an event or a resync (no comments, no retry)."""
        found: list[Frame] = []
        while len(found) < count:
            frame = await self.frame(seconds)
            if frame.field("event") is not None:
                found.append(frame)
        return found

    def feed(self, chunk: bytes) -> None:
        self.body += chunk
        self._buffer += chunk.decode()
        while "\n\n" in self._buffer:
            raw, self._buffer = self._buffer.split("\n\n", 1)
            self.frames.put_nowait(Frame(raw))


def cookie_header(client: httpx.AsyncClient) -> str:
    return "; ".join(f"{cookie.name}={cookie.value}" for cookie in client.cookies.jar)


@asynccontextmanager
async def open_sse(
    app: FastAPI,
    path: str,
    *,
    cookie: str | None = None,
    query: str = "",
    headers: dict[str, str] | None = None,
) -> AsyncIterator[SseResponse]:
    """GET ``path`` and keep the connection open until the block exits (a client disconnect)."""
    response = SseResponse()
    started, disconnect = asyncio.Event(), asyncio.Event()
    raw_headers = {"host": "testserver", "accept": "text/event-stream", **(headers or {})}
    if cookie:
        raw_headers["cookie"] = cookie
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": [(k.lower().encode(), v.encode()) for k, v in raw_headers.items()],
        "client": ("10.9.8.7", 50000),
        "server": ("testserver", 80),
    }

    async def receive() -> dict[str, Any]:
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            response.status = message["status"]
            response.headers = {k.decode(): v.decode() for k, v in message["headers"]}
            started.set()
        elif message["type"] == "http.response.body":
            response.feed(message.get("body", b""))

    task = asyncio.create_task(app(scope, receive, send))  # pyright: ignore[reportArgumentType]
    try:
        async with asyncio.timeout(5):
            await started.wait()
        yield response
    finally:
        disconnect.set()
        async with asyncio.timeout(5):
            await task
