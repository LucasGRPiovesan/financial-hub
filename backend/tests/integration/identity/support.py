"""Shared helpers for identity/projects API tests against real Postgres (as ``fh_app``).

Isolation: nothing can be deleted, so every test uses unique emails/slugs and its own client IP
(``10.x.y.z``), which keeps the per-email and per-IP login throttles of different tests apart.
The ``FrozenClock`` starts at the real current time so rows stay plausible next to ``now()``.
"""

import base64
import os
import secrets
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from financial_hub.cli import bootstrap_operator
from financial_hub.main import create_app
from financial_hub.modules.identity.application.api import (
    DEMO_EMAIL_DOMAIN,
    Role,
    grant_membership,
)
from financial_hub.modules.identity.application.service import PasswordHasher
from financial_hub.modules.identity.infrastructure.passwords import Argon2PasswordHasher
from financial_hub.platform.db import UnitOfWork
from financial_hub.platform.settings import Settings
from financial_hub.shared.clock import FrozenClock
from financial_hub.shared.ids import IdPrefix, parse_id

APP_URL_ENV = "FH_APP_TEST_DATABASE_URL"
PASSWORD = "correct horse battery"
BASE_URL = "http://testserver"
# Cheap Argon2id for tests that are not about the parameters themselves.
FAST_ARGON2: dict[str, int] = {"time_cost": 1, "memory_cost": 1024, "parallelism": 1}
# Deterministic test secrets: a pepper (32+ chars, not the dev default) and one 32-byte data key.
TEST_PEPPER = "test-pepper-0123456789abcdef-0123456789abcdef"
TEST_DATA_KEY = base64.b64encode(bytes([1]) * 32).decode()


def app_database_url() -> str:
    url = os.environ.get(APP_URL_ENV)
    if not url:
        pytest.fail(f"{APP_URL_ENV} is not set: run the suite with scripts/test.sh")
    return url


def unique_email(label: str = "op") -> str:
    return f"{label}-{secrets.token_hex(6)}@example.test"


def unique_demo_email(label: str = "demo") -> str:
    """An address inside the demo personas' domain (the only ones the demo login accepts).

    Prefixed `0` so it sorts first in the picker (owners by email) even in a well-used database.
    """
    return f"0{label}-{secrets.token_hex(6)}@{DEMO_EMAIL_DOMAIN}"


def unique_slug(label: str = "p") -> str:
    return f"{label}-{secrets.token_hex(6)}"


def random_ip() -> str:
    return "10." + ".".join(str(secrets.randbelow(254) + 1) for _ in range(3))


class SpyHasher:
    """Argon2id hasher that records the hash each ``verify`` ran against."""

    def __init__(self, inner: PasswordHasher | None = None) -> None:
        self.inner = inner or Argon2PasswordHasher(**FAST_ARGON2)
        self.verified: list[str] = []

    def hash(self, password: str) -> str:
        return self.inner.hash(password)

    def verify(self, password_hash: str, password: str) -> bool:
        self.verified.append(password_hash)
        return self.inner.verify(password_hash, password)

    def needs_rehash(self, password_hash: str) -> bool:
        return self.inner.needs_rehash(password_hash)

    @property
    def dummy_hash(self) -> str:
        return self.inner.dummy_hash


@dataclass
class Operator:
    id: str
    email: str
    password: str

    @property
    def uuid(self) -> UUID:
        return parse_id(self.id, IdPrefix.OPERATOR)


@dataclass
class Api:
    app: FastAPI
    clock: FrozenClock
    hasher: SpyHasher
    ip: str = field(default_factory=random_ip)

    @property
    def factory(self) -> async_sessionmaker[AsyncSession]:
        return self.app.state.session_factory

    def client(self, ip: str | None = None) -> httpx.AsyncClient:
        transport = httpx.ASGITransport(app=self.app, client=(ip or self.ip, 50000))
        return httpx.AsyncClient(transport=transport, base_url=BASE_URL)

    async def operator(
        self, *, email: str | None = None, password: str = PASSWORD, display_name: str = "Op"
    ) -> Operator:
        result = await bootstrap_operator(
            self.factory,
            email=email or unique_email(),
            display_name=display_name,
            password=password,
            hasher=self.hasher,
            clock=self.clock,
        )
        return Operator(id=result.operator.id, email=result.operator.email, password=password)

    async def grant(self, operator: Operator, project_id: str, role: Role) -> None:
        async with UnitOfWork(self.factory) as uow:
            await grant_membership(
                uow.session,
                operator_id=operator.uuid,
                project_id=parse_id(project_id, IdPrefix.PROJECT),
                role=role,
            )


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": SecretStr(app_database_url()),
        "app_env": "dev",
        "api_key_pepper": SecretStr(TEST_PEPPER),
        "encryption_keys": {1: SecretStr(TEST_DATA_KEY)},
        "otel_exporter_otlp_endpoint": None,
    }
    values.update(overrides)
    return Settings(**values)


@asynccontextmanager
async def running_api(*, hasher: SpyHasher | None = None, **settings: Any) -> AsyncGenerator[Api]:
    clock = FrozenClock(datetime.now(UTC))
    spy = hasher or SpyHasher()
    app = create_app(make_settings(**settings), clock=clock, password_hasher=spy)
    async with app.router.lifespan_context(app):
        yield Api(app=app, clock=clock, hasher=spy)


async def login(
    client: httpx.AsyncClient, operator: Operator, password: str | None = None
) -> httpx.Response:
    return await client.post(
        "/v1/auth/login",
        json={
            "email": operator.email,
            "password": operator.password if password is None else password,
        },
    )


async def logged_in(api: Api, operator: Operator) -> tuple[httpx.AsyncClient, str]:
    """A client holding a session of ``operator`` plus its CSRF token.

    The client is already open, so close it with ``aclosing``.
    """
    client = api.client()
    response = await login(client, operator)
    assert response.status_code == 200, response.text
    return client, response.json()["csrf_token"]


def csrf(token: str) -> dict[str, str]:
    return {"X-CSRF-Token": token}
