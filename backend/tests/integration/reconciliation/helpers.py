"""Builders for the reconciliation suites (real Postgres as ``fh_app``, the scriptable fake).

A :class:`Desk` is a payments :func:`shop` plus a second operator with the finance role (the
four-eyes approver) and the reconciliation service wired to the shop's own fake provider. Hub
payments are created "today"; :meth:`Desk.next_day` moves the frozen clock so that day becomes
yesterday, the default (and latest allowed) reconciliation window.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

import httpx

from financial_hub.modules.identity.application.api import Permission, Role
from financial_hub.modules.payments.application.api import ProviderRegistry
from financial_hub.modules.payments.infrastructure.fake_provider import FakeBehavior
from financial_hub.modules.reconciliation.application.api import (
    Break,
    ReconciliationService,
    Run,
    build_reconciliation_service,
)
from financial_hub.platform.http.auth import ProjectScope
from financial_hub.shared.pagination import PageParams
from tests.integration.identity.support import Api, Operator, csrf, logged_in
from tests.integration.payments.helpers import Shop, intent_uuid, shop

SCOPES = ("payments:write", "payments:refund")


@dataclass
class Desk:
    shop: Shop
    finance: Operator

    @property
    def api(self) -> Api:
        return self.shop.api

    @property
    def project_id(self) -> str:
        return self.shop.project_id

    @property
    def project_uuid(self) -> UUID:
        return self.shop.project_uuid

    @property
    def base(self) -> str:
        return f"/v1/projects/{self.project_id}/reconciliation"

    def service(self) -> ReconciliationService:
        return build_reconciliation_service(
            self.api.factory, clock=self.api.clock, registry=ProviderRegistry(fake=self.shop.fake)
        )

    def scope(self, operator: Operator | None = None) -> ProjectScope:
        """An operator session with every permission (the role checks are the HTTP tests')."""
        return ProjectScope(
            project_id=self.project_uuid,
            principal_kind="operator",
            principal_id=(operator or self.shop.owner).id,
            permissions=frozenset(Permission),
        )

    def next_day(self) -> None:
        self.api.clock.advance(timedelta(days=1))

    async def settled(self, amount: int = 1000, fee: int = 99) -> tuple[str, str, UUID]:
        """A captured and settled hub payment (R1-R4): ``(public id, provider id, intent)``."""
        created = await self.shop.create(amount=amount)
        assert created.status_code == 201, created.text
        view = created.json()
        provider_id, intent_id = view["provider_payment_id"], intent_uuid(view["id"])
        self.shop.fake.capture(provider_id, fee=fee)
        self.shop.fake.settle(provider_id)
        await self.shop.service().refresh(intent_id)
        return view["id"], provider_id, intent_id

    async def captured_at_creation(self, amount: int = 1000) -> tuple[str, str, UUID]:
        self.shop.fake.script(FakeBehavior.SUCCEED_CAPTURED)
        created = await self.shop.create(amount=amount)
        assert created.status_code == 201, created.text
        view = created.json()
        return view["id"], view["provider_payment_id"], intent_uuid(view["id"])

    async def run(self, kind: str) -> Run:
        return await self.service().start_run(
            self.scope(), kind=kind, window_start=None, window_end=None
        )

    async def breaks(self, run: Run | None = None, **filters: Any) -> list[Break]:
        run_ref = None if run is None else run_id(run)
        page = await self.service().list_breaks(
            self.project_uuid,
            PageParams(limit=100),
            kind=filters.get("kind"),
            state=filters.get("state"),
            run_id=run_ref,
        )
        return list(page.data)

    async def session(self, operator: Operator) -> tuple[httpx.AsyncClient, dict[str, str]]:
        """An open console client of ``operator`` and its CSRF header (close it yourself)."""
        client, token = await logged_in(self.api, operator)
        return client, csrf(token)


def run_id(run: Run) -> str:
    from financial_hub.shared.ids import IdPrefix, format_id

    return format_id(IdPrefix.RECONCILIATION_RUN, run.id)


@asynccontextmanager
async def desk(api: Api) -> AsyncIterator[Desk]:
    async with shop(api, scopes=SCOPES) as s:
        finance = await api.operator(display_name="Finance")
        await api.grant(finance, s.project_id, Role.FINANCE)
        yield Desk(s, finance)


async def member(d: Desk, role: Role) -> Operator:
    operator = await d.api.operator(display_name=role.value)
    await d.api.grant(operator, d.project_id, role)
    return operator
