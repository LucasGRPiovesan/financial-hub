"""A local webhook receiver and a driven sender for the webhooks suites (real Postgres, dev policy).

The receiver listens on ``127.0.0.1`` (allowlisted for these tests only, like
``FH_WEBHOOK_ALLOW_HOSTS`` in dev) and answers what the test tells it to. The sender is driven by
hand (``drain``) with the api's frozen clock, so retries are due only when the test advances it.
"""

import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx

from financial_hub.modules.webhooks.application.sender import SendStatus, WebhookSender
from financial_hub.modules.webhooks.domain import TargetPolicy
from financial_hub.modules.webhooks.infrastructure.http_client import GuardedSender
from tests.integration.identity.support import Api, Operator, csrf, logged_in
from tests.integration.projects.helpers import create_project

LOCAL_POLICY = TargetPolicy.for_environment(dev=True, allowed_hosts=frozenset({"127.0.0.1"}))
PENDING = {"pending", "leased"}


@dataclass
class Received:
    path: str
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)


@dataclass
class Receiver:
    """A minimal HTTP/1.1 server: records every request, answers ``status`` after ``delay``."""

    status: int = 200
    delay: float = 0.0
    headers: dict[str, str] = field(default_factory=dict[str, str])
    received: list[Received] = field(default_factory=list[Received])
    port: int = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/hook"

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            request_line, *lines = head.decode("latin-1").split("\r\n")
            headers = {
                name.strip().lower(): value.strip()
                for name, _, value in (line.partition(":") for line in lines if line)
            }
            body = await reader.readexactly(int(headers.get("content-length", "0")))
            self.received.append(Received(request_line.split(" ")[1], headers, body))
            if self.delay:
                await asyncio.sleep(self.delay)
            extra = "".join(f"{name}: {value}\r\n" for name, value in self.headers.items())
            writer.write(
                f"HTTP/1.1 {self.status} X\r\nContent-Length: 2\r\nConnection: close\r\n"
                f"{extra}\r\nok".encode()
            )
            await writer.drain()
        except asyncio.IncompleteReadError, ConnectionError:
            pass
        finally:
            writer.close()


@asynccontextmanager
async def receiver() -> AsyncGenerator[Receiver]:
    target = Receiver()
    server = await asyncio.start_server(target.handle, "127.0.0.1", 0)
    target.port = server.sockets[0].getsockname()[1]
    try:
        yield target
    finally:
        server.close()
        server.close_clients()
        await server.wait_closed()


@dataclass
class Hooks:
    """A project with a logged-in owner, a local receiver and a sender bound to the api."""

    api: Api
    owner: Operator
    client: httpx.AsyncClient
    csrf_token: str
    project_id: str
    target: Receiver
    timeout_seconds: float = 5.0

    @property
    def path(self) -> str:
        return f"/v1/projects/{self.project_id}/webhook-endpoints"

    def sender(self) -> WebhookSender:
        return WebhookSender(
            self.api.factory,
            keyring=self.api.app.state.keyring,
            clock=self.api.clock,
            client=GuardedSender(LOCAL_POLICY, timeout_seconds=self.timeout_seconds),
            batch_size=100,
            uniform=lambda: 0.5,
        )

    async def create(self, **overrides: Any) -> dict[str, Any]:
        body: dict[str, Any] = {"url": self.target.url, "event_types": ["*"]}
        body.update(overrides)
        response = await self.client.post(self.path, json=body, headers=csrf(self.csrf_token))
        assert response.status_code == 201, response.text
        return response.json()

    async def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self.client.post(path, headers=csrf(self.csrf_token), **kwargs)

    async def ping(self, endpoint_id: str) -> str:
        response = await self.post(f"{self.path}/{endpoint_id}/test")
        assert response.status_code == 202, response.text
        return response.json()["event_id"]

    async def deliveries(self, event_id: str) -> list[dict[str, Any]]:
        response = await self.client.get(
            f"/v1/projects/{self.project_id}/events/{event_id}/deliveries"
        )
        assert response.status_code == 200, response.text
        return [row for row in response.json() if row["kind"] == "webhook"]

    async def delivery(self, event_id: str) -> dict[str, Any]:
        (row,) = await self.deliveries(event_id)
        return row

    async def drain(self, event_id: str, rounds: int = 5) -> dict[str, Any]:
        """Run the sender until the event's delivery left ``pending``/``leased`` (or rounds end).

        Other suites' leftover deliveries in the shared database may be claimed too; they fail
        fast against closed local ports and are rescheduled, so they never block this one.
        """
        sender = self.sender()
        row = await self.delivery(event_id)
        for _ in range(rounds):
            statuses = await sender.run_once()
            row = await self.delivery(event_id)
            if row["status"] not in PENDING or not statuses:
                break
        return row

    async def advance(self, delta: timedelta) -> None:
        """Move the frozen clock; the console session may expire, so sign in again."""
        self.api.clock.advance(delta)
        await self.client.aclose()
        self.client, self.csrf_token = await logged_in(self.api, self.owner)

    async def attempts(self, endpoint_id: str) -> list[dict[str, Any]]:
        response = await self.client.get(f"{self.path}/{endpoint_id}/attempts")
        assert response.status_code == 200, response.text
        return response.json()["data"]


@asynccontextmanager
async def hooks(api: Api) -> AsyncGenerator[Hooks]:
    api.app.state.webhook_policy = LOCAL_POLICY
    owner = await api.operator()
    project = await create_project(api, owner, "Webhook shop")
    async with receiver() as target:
        client, csrf_token = await logged_in(api, owner)
        state = Hooks(api, owner, client, csrf_token, project["id"], target)
        try:
            yield state
        finally:
            await state.client.aclose()  # advance() may have replaced the first one


__all__ = ["Hooks", "Receiver", "SendStatus", "hooks", "receiver"]
