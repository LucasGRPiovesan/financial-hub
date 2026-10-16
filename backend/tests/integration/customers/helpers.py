"""Builders for the customers suites (real Postgres, ``fh_app``)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx

from tests.integration.identity.support import Api, Operator, csrf, logged_in
from tests.integration.projects.helpers import bearer, create_key, create_project


@dataclass
class Tenant:
    """A project with an owner session and an API key holding ``customers:write``."""

    api: Api
    owner: Operator
    client: httpx.AsyncClient
    csrf_token: str
    project: dict[str, str]
    write_key: str

    @property
    def project_id(self) -> str:
        return self.project["id"]

    @property
    def base(self) -> str:
        return f"/v1/projects/{self.project_id}/customers"

    async def put(
        self, ref: str, *, via_key: bool = True, headers: dict[str, str] | None = None, **body: Any
    ) -> httpx.Response:
        payload: dict[str, Any] = {"name": "Maria Silva", "kind": "individual", **body}
        if via_key:
            async with self.api.client() as client:
                return await client.put(
                    f"{self.base}/{ref}",
                    json=payload,
                    headers={**bearer(self.write_key), **(headers or {})},
                )
        return await self.client.put(
            f"{self.base}/{ref}",
            json=payload,
            headers={**csrf(self.csrf_token), **(headers or {})},
        )

    async def get(self, path: str = "", *, key: str | None = None, **params: Any) -> httpx.Response:
        async with self.api.client() as client:
            return await client.get(
                f"{self.base}{path}", params=params, headers=bearer(key or self.write_key)
            )


@asynccontextmanager
async def tenant(api: Api, name: str = "Shop") -> AsyncIterator[Tenant]:
    owner = await api.operator()
    project = await create_project(api, owner, name)
    client, csrf_token = await logged_in(api, owner)
    try:
        key = await create_key(
            client,
            csrf_token,
            project["id"],
            scopes=["customers:write", "customers:read"],
        )
        yield Tenant(api, owner, client, csrf_token, project, key["key"])
    finally:
        await client.aclose()
