"""Builders for the metering suites (real Postgres as ``fh_app``, HTTP through the ASGI app)."""

import itertools
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from financial_hub.shared.ids import IdPrefix, parse_id
from tests.integration.identity.support import Api, Operator, csrf, logged_in
from tests.integration.projects.helpers import bearer, create_key, create_project

_sequence = itertools.count(1)


def transaction_id(prefix: str = "evt") -> str:
    return f"{prefix}-{next(_sequence)}-{id(_sequence) % 9973}"


@dataclass
class Meter:
    """A project with a customer, a key that records usage and an owner session for the console."""

    api: Api
    owner: Operator
    owner_client: httpx.AsyncClient
    csrf_token: str
    project: dict[str, str]
    key: str
    customer_id: str

    @property
    def project_id(self) -> str:
        return self.project["id"]

    @property
    def project_uuid(self) -> UUID:
        return parse_id(self.project_id, IdPrefix.PROJECT)

    @property
    def metrics_path(self) -> str:
        return f"/v1/projects/{self.project_id}/metrics"

    async def metric(self, code: str, aggregation: str = "count", field: str | None = None) -> Any:
        response = await self.owner_client.post(
            self.metrics_path,
            json={"code": code, "name": code.title(), "aggregation": aggregation, "field": field},
            headers=csrf(self.csrf_token),
        )
        assert response.status_code == 201, response.text
        return response.json()

    def event(self, metric: str, **overrides: Any) -> dict[str, Any]:
        values: dict[str, Any] = {
            "transaction_id": transaction_id(),
            "metric": metric,
            "customer_external_ref": "buyer-1",
            "properties": {},
        }
        values.update(overrides)
        return values

    async def ingest(
        self, body: dict[str, Any], *, key: str | None = None, path: str = "/v1/usage"
    ) -> httpx.Response:
        async with self.api.client() as client:
            return await client.post(path, json=body, headers=bearer(key or self.key))

    async def batch(
        self, events: list[dict[str, Any]], *, key: str | None = None
    ) -> httpx.Response:
        return await self.ingest({"events": events}, key=key, path="/v1/usage/batch")

    async def summary(
        self, metric: str, start: datetime, end: datetime, *, customer_id: str | None = None
    ) -> httpx.Response:
        return await self.owner_client.get(
            f"/v1/projects/{self.project_id}/usage/summary",
            params={
                "customer_id": customer_id or self.customer_id,
                "metric": metric,
                "from": start.isoformat(),
                "to": end.isoformat(),
            },
        )


@asynccontextmanager
async def meter(api: Api, *, scopes: tuple[str, ...] = ("usage:write",)) -> AsyncIterator[Meter]:
    owner = await api.operator()
    project = await create_project(api, owner, "Metering shop")
    client, csrf_token = await logged_in(api, owner)
    try:
        key = await create_key(
            client, csrf_token, project["id"], scopes=[*scopes, "customers:write"]
        )
        async with api.client() as key_client:
            customer = await key_client.put(
                f"/v1/projects/{project['id']}/customers/buyer-1",
                json={"name": "Maria Silva", "kind": "individual"},
                headers=bearer(key["key"]),
            )
        assert customer.status_code == 201, customer.text
        yield Meter(api, owner, client, csrf_token, project, key["key"], customer.json()["id"])
    finally:
        await client.aclose()


async def scalar(engine: AsyncEngine, sql: str, **params: object) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar_one()


async def events_of(engine: AsyncEngine, project: UUID, subject: str) -> list[tuple[str, Any]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT type, data FROM events WHERE project_id = :project AND subject = :subject "
                "ORDER BY recorded_at, id"
            ),
            {"project": project, "subject": subject},
        )
        return [(row.type, row.data) for row in rows.all()]
