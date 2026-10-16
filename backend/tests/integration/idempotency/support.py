"""A probe router whose handler counts its side effects, mounted on the real app.

``POST /test/idempotency/{optional,required}`` records ``value`` in ``effects`` (the side effect)
and answers ``status`` with a JSON body; ``delay_ms`` keeps the request in flight, ``explode``
raises after the effect, ``pad`` grows the body.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass, field
from typing import Annotated, Any

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from financial_hub.platform.http.auth import Principal, authenticated
from financial_hub.platform.http.protection.idempotency import idempotent
from tests.integration.identity.support import BASE_URL, Api, csrf, logged_in, running_api

PROBE = "/test/idempotency"


class ProbeBody(BaseModel):
    value: str = "effect"
    status: int = 201
    delay_ms: int = 0
    explode: bool = False
    pad: int = 0


def probe_router(effects: list[str]) -> APIRouter:
    router = APIRouter(prefix=PROBE)

    async def run(body: ProbeBody) -> JSONResponse:
        effects.append(body.value)
        if body.delay_ms:
            await asyncio.sleep(body.delay_ms / 1000)
        if body.explode:
            raise RuntimeError("probe exploded after its side effect")
        return JSONResponse(
            {"value": body.value, "effect": len(effects), "pad": "x" * body.pad},
            status_code=body.status,
            headers={"Location": f"{PROBE}/{len(effects)}", "X-Not-Stored": "1"},
        )

    @router.post("/optional")
    async def optional(  # pyright: ignore[reportUnusedFunction]
        body: ProbeBody,
        _principal: Annotated[Principal, authenticated()],
        _idempotency: Annotated[None, idempotent()],
    ) -> JSONResponse:
        return await run(body)

    @router.post("/required")
    async def required(  # pyright: ignore[reportUnusedFunction]
        body: ProbeBody,
        _principal: Annotated[Principal, authenticated()],
        _idempotency: Annotated[None, idempotent("required")],
    ) -> JSONResponse:
        return await run(body)

    return router


@dataclass
class Probe:
    api: Api
    client: httpx.AsyncClient
    csrf_token: str
    effects: list[str] = field(default_factory=list[str])

    def headers(self, key: str | None) -> dict[str, str]:
        headers = csrf(self.csrf_token)
        if key is not None:
            headers["Idempotency-Key"] = key
        return headers

    async def post(
        self, key: str | None, *, route: str = "optional", **body: Any
    ) -> httpx.Response:
        return await self.client.post(f"{PROBE}/{route}", json=body, headers=self.headers(key))

    def lenient_client(self) -> httpx.AsyncClient:
        """Same session, but unhandled app exceptions come back as the 500 the server sends."""
        transport = httpx.ASGITransport(
            app=self.api.app, raise_app_exceptions=False, client=(self.api.ip, 50000)
        )
        return httpx.AsyncClient(
            transport=transport, base_url=BASE_URL, cookies=self.client.cookies
        )


@asynccontextmanager
async def running_probe(**settings: Any) -> AsyncIterator[Probe]:
    async with running_api(**settings) as api:
        effects: list[str] = []
        api.app.include_router(probe_router(effects))
        operator = await api.operator()
        client, csrf_token = await logged_in(api, operator)
        async with aclosing(client):
            yield Probe(api=api, client=client, csrf_token=csrf_token, effects=effects)
