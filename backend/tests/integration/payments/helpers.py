"""Builders for the payments suites (real Postgres as ``fh_app``, the scriptable fake provider)."""

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from financial_hub.modules.payments.application.ledger_postings import (
    PaymentChange,
    post_payment_changes,
)
from financial_hub.modules.payments.application.registry import ProviderRegistry
from financial_hub.modules.payments.application.service import DUE_DATE_TZ, PaymentService
from financial_hub.modules.payments.domain import CaptureStatus
from financial_hub.modules.payments.infrastructure.fake_provider import FakePaymentProvider
from financial_hub.platform.db import UnitOfWork
from financial_hub.platform.http.auth import ProjectScope
from financial_hub.shared.ids import IdPrefix, parse_id
from tests.integration.identity.support import Api, Operator, logged_in
from tests.integration.projects.helpers import bearer, create_key, create_project


@dataclass
class Confirmations:
    """Wraps the real ledger hook: records each capture it sees as ``(provider id, late)``."""

    calls: list[tuple[str, bool]] = field(default_factory=list[tuple[str, bool]])

    async def __call__(self, uow: UnitOfWork, change: PaymentChange) -> None:
        assert uow.session.in_transaction()
        before, after = change.before.capture_status, change.payment.state.capture_status
        if before is not CaptureStatus.CAPTURED and after is CaptureStatus.CAPTURED:
            late = before in (CaptureStatus.FAILED, CaptureStatus.CANCELED)
            self.calls.append((change.payment.provider_payment_id, late))
        await post_payment_changes(uow, change)


@dataclass
class Shop:
    """A project with a customer, a key holding every payments scope, and its own fake provider."""

    api: Api
    owner: Operator
    project: dict[str, str]
    key: str
    customer_id: str
    fake: FakePaymentProvider
    confirmations: Confirmations

    @property
    def project_id(self) -> str:
        return self.project["id"]

    @property
    def project_uuid(self) -> UUID:
        return parse_id(self.project_id, IdPrefix.PROJECT)

    @property
    def base(self) -> str:
        return f"/v1/projects/{self.project_id}/payments"

    def service(self, registry: ProviderRegistry | None = None) -> PaymentService:
        return PaymentService(
            self.api.factory,
            clock=self.api.clock,
            registry=registry or ProviderRegistry(fake=self.fake),
            ledger_hook=self.confirmations,
        )

    def scope(self) -> ProjectScope:
        return ProjectScope(
            project_id=self.project_uuid,
            principal_kind="api_key",
            principal_id="test",
            permissions=frozenset(),
        )

    def body(self, **overrides: Any) -> dict[str, Any]:
        due = self.api.clock.now().astimezone(DUE_DATE_TZ).date() + timedelta(days=3)
        values: dict[str, Any] = {
            "amount": 1000,
            "currency": "BRL",
            "method": "pix",
            "due_date": due.isoformat(),
            "customer": {"id": self.customer_id},
        }
        values.update(overrides)
        return values

    async def create(
        self, *, key: str | None = None, idempotency_key: str | None = None, **overrides: Any
    ) -> httpx.Response:
        async with self.api.client() as client:
            return await client.post(
                self.base,
                json=self.body(**overrides),
                headers={
                    **bearer(key or self.key),
                    "Idempotency-Key": idempotency_key or secrets.token_hex(8),
                },
            )

    async def refund(
        self, payment_id: str, *, key: str | None = None, **body: Any
    ) -> httpx.Response:
        async with self.api.client() as client:
            return await client.post(
                f"{self.base}/{payment_id}/refunds",
                json=body,
                headers={**bearer(key or self.key), "Idempotency-Key": secrets.token_hex(8)},
            )

    async def get(self, path: str = "", *, key: str | None = None, **params: Any) -> httpx.Response:
        async with self.api.client() as client:
            return await client.get(
                f"{self.base}{path}", params=params, headers=bearer(key or self.key)
            )


@asynccontextmanager
async def shop(
    api: Api, *, scopes: tuple[str, ...] = ("payments:write", "payments:refund")
) -> AsyncIterator[Shop]:
    fake = FakePaymentProvider(namespace=secrets.token_hex(4))
    api.app.state.payment_providers = ProviderRegistry(fake=fake)
    owner = await api.operator()
    project = await create_project(api, owner, "Payments shop")
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
        yield Shop(api, owner, project, key["key"], customer.json()["id"], fake, Confirmations())
    finally:
        await client.aclose()


def intent_uuid(payment_id: str) -> UUID:
    return parse_id(payment_id, IdPrefix.PAYMENT_INTENT)


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
