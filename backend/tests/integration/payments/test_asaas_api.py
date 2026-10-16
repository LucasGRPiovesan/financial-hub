"""Payments through the API with the Asaas adapter over a scripted sandbox (PRO-41).

Real Postgres, real provider-accounts API and credentials vault; only the network is replaced
(``httpx.MockTransport`` behind the registry's ``asaas`` factory). Proves that the credentials come
from the project's active account, and the ADR-0012 protocol end to end: a create whose answer is
lost converges through the external-reference lookup, without a duplicate charge.
"""

import json
from contextlib import aclosing
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from financial_hub.modules.payments.application.registry import (
    ProviderRegistry,
    projects_credentials_loader,
)
from financial_hub.modules.payments.application.service import PaymentService
from financial_hub.modules.payments.domain import CreationStatus
from financial_hub.modules.payments.infrastructure.asaas_provider import asaas_factory
from tests.integration.identity.support import Api, csrf, logged_in
from tests.integration.payments.helpers import Shop, intent_uuid, scalar, shop
from tests.integration.projects.helpers import create_account

FAKE_KEY = "fake-asaas-sandbox-key-7c1d9e"
ASAAS_CUSTOMER = "cus_000005118001"


class SandboxAsaas:
    """A tiny stateful Asaas Sandbox: payments by id, lookups by externalReference."""

    def __init__(self) -> None:
        self.payments: dict[str, dict[str, Any]] = {}
        self.keys: set[str | None] = set()
        self.post_count = 0
        self.customer_lookups: list[str] = []
        self.fail_next_post: Exception | None = None
        self.store_before_failing = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.keys.add(request.headers.get("access_token"))
        path = request.url.path.removeprefix("/v3/")
        params = request.url.params
        if request.method == "GET" and path == "customers":
            reference = params["externalReference"]
            self.customer_lookups.append(reference)
            return self._page([{"id": ASAAS_CUSTOMER, "externalReference": reference}])
        if request.method == "GET" and path == "payments":
            wanted = params["externalReference"]
            return self._page(
                [p for p in self.payments.values() if p["externalReference"] == wanted]
            )
        if request.method == "GET" and path.startswith("payments/"):
            payment = self.payments.get(path.removeprefix("payments/"))
            if payment is None:
                return httpx.Response(404, json={"errors": [{"code": "not_found"}]})
            return self._json(payment)
        if request.method == "POST" and path == "payments":
            self.post_count += 1
            body = json.loads(request.content)
            created = self._new(body)
            failure, self.fail_next_post = self.fail_next_post, None
            if failure is not None:
                if self.store_before_failing:
                    self.payments[created["id"]] = created
                raise failure
            self.payments[created["id"]] = created
            return self._json(created)
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    def _new(self, body: dict[str, Any]) -> dict[str, Any]:
        assert body["customer"] == ASAAS_CUSTOMER
        return {
            "object": "payment",
            "id": f"pay_{len(self.payments) + 1:06d}",
            "status": "PENDING",
            "value": json.loads(json.dumps(body["value"])),
            "netValue": body["value"],
            "billingType": body["billingType"],
            "externalReference": body["externalReference"],
            "deleted": False,
        }

    @staticmethod
    def _json(body: dict[str, Any]) -> httpx.Response:
        return httpx.Response(200, json=body)

    def _page(self, items: list[dict[str, Any]]) -> httpx.Response:
        return httpx.Response(200, json={"hasMore": False, "data": items})


def wire(api: Api, asaas: SandboxAsaas) -> ProviderRegistry:
    """What main.py does, with the network replaced: the asaas factory and the credentials door."""
    registry = ProviderRegistry(
        factories={"asaas": asaas_factory(transport=httpx.MockTransport(asaas))}
    )
    api.app.state.payment_providers = registry
    api.app.state.payment_credentials = projects_credentials_loader(api.app.state.keyring)
    return registry


def service_for(s: Shop, registry: ProviderRegistry) -> PaymentService:
    return PaymentService(
        s.api.factory,
        clock=s.api.clock,
        registry=registry,
        credentials=projects_credentials_loader(s.api.app.state.keyring),
    )


async def account_of(s: Shop, *, environment: str = "sandbox", name: str = "main") -> str:
    client, token = await logged_in(s.api, s.owner)
    async with aclosing(client):
        account = await create_account(
            client,
            token,
            s.project_id,
            name=name,
            environment=environment,
            credentials={"api_key": FAKE_KEY},
        )
    return account["id"]


async def set_active(s: Shop, account_id: str, *, active: bool) -> None:
    client, token = await logged_in(s.api, s.owner)
    async with aclosing(client):
        response = await client.post(
            f"/v1/projects/{s.project_id}/provider-accounts/{account_id}/"
            f"{'activate' if active else 'deactivate'}",
            headers=csrf(token),
        )
    assert response.status_code == 200, response.text


async def creation_status(engine: AsyncEngine, payment_id: str) -> str:
    return await scalar(
        engine,
        "SELECT creation_status FROM payment_intents WHERE id = :id",
        id=intent_uuid(payment_id),
    )


# --- create -> captured through the account's own credentials -------------------------------------


async def test_a_payment_is_created_at_asaas_with_the_accounts_credentials(api: Api) -> None:
    asaas = SandboxAsaas()
    async with shop(api) as s:
        wire(api, asaas)
        account_id = await account_of(s)

        created = await s.create(provider_account_id=account_id, amount=12345, method="boleto")
        body = created.json()
        fetched = (await s.get(f"/{body['id']}")).json()

    assert created.status_code == 201, created.text
    assert body["provider"] == "asaas" and body["provider_account_id"] == account_id
    assert body["status"] == "requires_payment"
    assert fetched["amount"] == 12345
    assert asaas.keys == {FAKE_KEY}  # the project's decrypted key, nothing else
    assert asaas.post_count == 1
    [remote] = asaas.payments.values()
    assert remote["externalReference"] == body["id"]  # our pi_ id travels as externalReference
    assert remote["value"] == 123.45 and remote["billingType"] == "BOLETO"
    assert len(asaas.customer_lookups) == 1 and asaas.customer_lookups[0].startswith("cus_")
    assert FAKE_KEY not in created.text


async def test_a_payment_reported_received_is_captured_and_settled(api: Api) -> None:
    asaas = SandboxAsaas()
    async with shop(api) as s:
        registry = wire(api, asaas)
        account_id = await account_of(s)
        view = (await s.create(provider_account_id=account_id)).json()
        [remote] = asaas.payments.values()
        remote["status"] = "RECEIVED"
        refreshed = await service_for(s, registry).refresh(intent_uuid(view["id"]))
        after = (await s.get(f"/{view['id']}")).json()

    assert refreshed is not None and refreshed.value == "settled"
    assert after["status"] == "settled"
    assert after["captured_amount"] == after["amount"]


# --- a lost answer converges through the lookup ---------------------------------------------------


async def test_a_timeout_after_the_post_converges_through_the_lookup_without_a_duplicate(
    api: Api, app_engine: AsyncEngine
) -> None:
    asaas = SandboxAsaas()
    asaas.fail_next_post = httpx.ReadTimeout("response lost")
    asaas.store_before_failing = True  # Asaas created the charge; only the answer never arrived
    async with shop(api) as s:
        registry = wire(api, asaas)
        account_id = await account_of(s)

        created = await s.create(provider_account_id=account_id)
        view = created.json()
        before = await creation_status(app_engine, view["id"])
        service = service_for(s, registry)
        resolved = await service.resolve_payment(intent_uuid(view["id"]))
        again = await service.resolve_payment(intent_uuid(view["id"]))
        after = (await s.get(f"/{view['id']}")).json()

    assert created.status_code == 201 and view["status"] == "processing"
    assert before == "unknown"
    assert resolved is again is CreationStatus.CREATED
    assert after["status"] == "requires_payment"
    assert asaas.post_count == 1  # never blind-retried
    assert len(asaas.payments) == 1  # and no second charge exists
    count = await scalar(
        app_engine,
        "SELECT count(*) FROM provider_payments WHERE payment_intent_id = :id",
        id=intent_uuid(view["id"]),
    )
    assert count == 1


async def test_a_refused_connection_is_resent_only_after_the_lookup_confirms_absence(
    api: Api, app_engine: AsyncEngine
) -> None:
    asaas = SandboxAsaas()
    asaas.fail_next_post = httpx.ConnectError("no route")
    async with shop(api) as s:
        registry = wire(api, asaas)
        account_id = await account_of(s)

        view = (await s.create(provider_account_id=account_id)).json()
        resolved = await service_for(s, registry).resolve_payment(intent_uuid(view["id"]))

    assert view["status"] == "processing"
    assert resolved is CreationStatus.CREATED
    assert asaas.post_count == 2  # the failed attempt, then one send under a new token
    assert len(asaas.payments) == 1


# --- accounts that must not take payments ---------------------------------------------------------


async def test_an_inactive_account_is_refused_and_asaas_is_never_called(api: Api) -> None:
    asaas = SandboxAsaas()
    async with shop(api) as s:
        wire(api, asaas)
        account_id = await account_of(s)
        await set_active(s, account_id, active=False)

        refused = await s.create(provider_account_id=account_id)

    assert refused.status_code == 422
    error = refused.json()["errors"][0]
    assert (error["pointer"], error["code"]) == ("/provider_account_id", "not_found")
    assert asaas.keys == set() and asaas.post_count == 0


async def test_another_projects_account_is_refused_and_its_key_is_never_used(api: Api) -> None:
    asaas = SandboxAsaas()
    async with shop(api) as s, shop(api) as other:
        wire(api, asaas)
        foreign = await account_of(other)

        refused = await s.create(provider_account_id=foreign)

    assert refused.status_code == 422
    assert refused.json()["errors"][0]["code"] == "not_found"
    assert asaas.keys == set()


async def test_an_account_disabled_after_the_send_keeps_the_outcome_unknown(api: Api) -> None:
    asaas = SandboxAsaas()
    asaas.fail_next_post = httpx.ReadTimeout("response lost")
    asaas.store_before_failing = True
    async with shop(api) as s:
        registry = wire(api, asaas)
        account_id = await account_of(s)
        view = (await s.create(provider_account_id=account_id)).json()
        await set_active(s, account_id, active=False)
        waiting = await service_for(s, registry).resolve_payment(intent_uuid(view["id"]))
        await set_active(s, account_id, active=True)
        resolved = await service_for(s, registry).resolve_payment(intent_uuid(view["id"]))

    # credentials_unavailable: the resolver waits instead of guessing, then converges
    assert waiting is CreationStatus.UNKNOWN
    assert resolved is CreationStatus.CREATED
    assert len(asaas.payments) == 1


async def test_a_production_account_is_refused_because_only_the_sandbox_is_wired(
    api: Api,
) -> None:
    asaas = SandboxAsaas()
    async with shop(api) as s:
        wire(api, asaas)
        account_id = await account_of(s, environment="production")

        refused = await s.create(provider_account_id=account_id)

    assert refused.status_code == 422, refused.text
    assert refused.json()["errors"][0]["code"] == "environment_not_supported"
    assert asaas.keys == set()


async def test_without_a_credentials_loader_a_real_account_cannot_take_payments(api: Api) -> None:
    asaas = SandboxAsaas()
    async with shop(api) as s:
        wire(api, asaas)
        api.app.state.payment_credentials = None
        account_id = await account_of(s)

        refused = await s.create(provider_account_id=account_id)

    assert refused.status_code == 422
    assert refused.json()["errors"][0]["code"] == "credentials_unavailable"
