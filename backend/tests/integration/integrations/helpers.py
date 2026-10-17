"""Builders for the integrations suites (real Postgres as ``fh_app``, HTTP through the ASGI app).

A ``Store`` is a metering project (customer ``buyer-1``, a key with ``events:write``) plus what the
sample manifest references: the ``api_calls`` metric (sums ``units``) and the ``demo-api-calls``
price. A finance member is the second pair of eyes.
"""

import copy
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from financial_hub.modules.identity.application.api import Role
from tests.integration.identity.support import Api, csrf
from tests.integration.metering.helpers import Meter, meter, scalar
from tests.integration.projects.helpers import bearer, member
from tests.integration.webhooks.helpers import LOCAL_POLICY

HOOK_URL = "http://127.0.0.1:9/manifest-hook"
EVENT = "consultation.completed"

MANIFEST: dict[str, Any] = {
    "schema_version": "manifest.v1",
    "description": "Demo Store consultations",
    "product": {"code": "demo-store", "name": "Demo Store"},
    "customers": {"external_id": "/customer/id", "name": "/customer/name"},
    "business_events": [
        {
            "name": EVENT,
            "payload_schema": {
                "type": "object",
                "required": ["customer", "consultation_id", "minutes"],
                "properties": {
                    "customer": {
                        "type": "object",
                        "required": ["id"],
                        "properties": {
                            "id": {"type": "string", "maxLength": 128},
                            "name": {"type": "string"},
                        },
                    },
                    "consultation_id": {"type": "string", "maxLength": 64},
                    "minutes": {"type": "integer", "minimum": 1, "maximum": 600},
                },
            },
            "usage": [
                {
                    "metric": "api_calls",
                    "quantity": {"op": "multiply", "of": "/minutes", "by": "1"},
                },
                {"metric": "consultations"},
            ],
        }
    ],
    "metrics": [
        {"source": "existing", "code": "api_calls"},
        {
            "source": "create",
            "code": "consultations",
            "name": "Consultations",
            "aggregation": "count",
        },
    ],
    "prices": [{"code": "demo-api-calls"}],
    "splits": [{"payee": "clinic-1", "basis_points": 7000}],
    "wallet": {"mode": "disabled"},
    "webhooks": [{"url": HOOK_URL, "event_types": ["usage.*"]}],
}

PAYLOAD: dict[str, Any] = {
    "customer": {"id": "buyer-1", "name": "Maria Silva"},
    "consultation_id": "c-1",
    "minutes": 30,
}


def manifest(**changes: Any) -> dict[str, Any]:
    document = copy.deepcopy(MANIFEST)
    document.update(copy.deepcopy(changes))
    return document


@dataclass
class Store:
    meter: Meter
    finance: httpx.AsyncClient
    finance_csrf: str

    @property
    def owner(self) -> httpx.AsyncClient:
        return self.meter.owner_client

    @property
    def base(self) -> str:
        return f"/v1/projects/{self.meter.project_id}/manifests"

    def as_owner(self) -> tuple[httpx.AsyncClient, dict[str, str]]:
        return self.owner, csrf(self.meter.csrf_token)

    def as_finance(self) -> tuple[httpx.AsyncClient, dict[str, str]]:
        return self.finance, csrf(self.finance_csrf)

    async def create(self, content: dict[str, Any]) -> dict[str, Any]:
        client, headers = self.as_owner()
        response = await client.post(self.base, json={"content": content}, headers=headers)
        assert response.status_code == 201, response.text
        return response.json()

    async def validate(self, version: int) -> dict[str, Any]:
        client, headers = self.as_owner()
        response = await client.post(f"{self.base}/{version}/validate", headers=headers)
        assert response.status_code == 200, response.text
        return response.json()

    async def approve(
        self, version: int, content_hash: str, *, by_finance: bool = False
    ) -> httpx.Response:
        client, headers = self.as_finance() if by_finance else self.as_owner()
        return await client.post(
            f"{self.base}/{version}/approve", json={"content_hash": content_hash}, headers=headers
        )

    async def activate(self, version: int, *, by_finance: bool = False) -> httpx.Response:
        client, headers = self.as_finance() if by_finance else self.as_owner()
        return await client.post(f"{self.base}/{version}/activate", headers=headers)

    async def live(self, content: dict[str, Any]) -> dict[str, Any]:
        """Create, validate, approve (by finance) and activate ``content``; the activation body."""
        created = await self.create(content)
        validated = await self.validate(created["version"])
        approved = await self.approve(
            created["version"], validated["content_hash"], by_finance=True
        )
        assert approved.status_code == 200, approved.text
        activated = await self.activate(created["version"], by_finance=True)
        assert activated.status_code == 200, activated.text
        return activated.json()

    async def send(self, body: dict[str, Any], *, key: str | None = None) -> httpx.Response:
        async with self.meter.api.client() as client:
            return await client.post(
                "/v1/business-events", json=body, headers=bearer(key or self.meter.key)
            )


@asynccontextmanager
async def store(api: Api, *, scopes: tuple[str, ...] = ("events:write",)) -> AsyncIterator[Store]:
    api.app.state.webhook_policy = LOCAL_POLICY
    async with meter(api, scopes=scopes) as m:
        await m.metric("api_calls", "sum", "units")
        client, headers = m.owner_client, csrf(m.csrf_token)
        price = await client.post(
            f"/v1/projects/{m.project_id}/prices",
            json={
                "code": "demo-api-calls",
                "name": "API calls",
                "currency": "BRL",
                "metric_code": "api_calls",
                "model": {"kind": "per_unit", "unit_price": "0.10"},
                "effective_from": "2026-01-01T00:00:00Z",
            },
            headers=headers,
        )
        assert price.status_code == 201, price.text
        _, finance, finance_csrf = await member(api, m.project_id, Role.FINANCE)
        try:
            yield Store(m, finance, finance_csrf)
        finally:
            await finance.aclose()


async def count(engine: AsyncEngine, table: str, project: object) -> int:
    return await scalar(
        engine,
        f"SELECT count(*) FROM public.{table} WHERE project_id = :project",  # noqa: S608 - fixed names
        project=project,
    )
