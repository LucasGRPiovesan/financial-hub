"""Asaas customer provisioning before the first charge (W4.4, ADR-0012 s3).

The charge is never sent without a provider customer the hub has linked. Provisioning follows the
same external-call protocol as charges: a lease (fencing token) before the call, the hub's
``cus_`` id as ``externalReference``, and a lookup first, so a lost answer or a crash converges on
the one customer Asaas holds instead of creating a second one. The decrypted CPF/CNPJ only travels
in the request body and never reaches a log or a response.
"""

import json
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from structlog.testing import capture_logs

from financial_hub.modules.payments.application.service import DEFAULT_LEASE
from financial_hub.shared.ids import IdPrefix, new_uuid, parse_id
from tests.integration.identity.support import Api
from tests.integration.payments.helpers import Shop, intent_uuid, scalar, shop
from tests.integration.payments.test_asaas_api import (
    SandboxAsaas,
    account_of,
    creation_status,
    service_for,
    wire,
)
from tests.integration.payments.test_asaas_webhooks import processor_for
from tests.integration.projects.helpers import bearer

TEST_CPF = "24971563792"  # a valid test CPF (the same one the live test sends to the sandbox)


class CustomerAsaas(SandboxAsaas):
    """The sandbox with a customer store: lookup by ``externalReference``, ``POST`` creates."""

    def __init__(self) -> None:
        super().__init__()
        self.customers: dict[str, dict[str, Any]] = {}
        self.customer_posts: list[dict[str, Any]] = []
        self.fail_next_customer_post: Exception | None = None
        self.store_customer_before_failing = False
        self.reject_customers = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != "/v3/customers":
            return super().__call__(request)
        self.keys.add(request.headers.get("access_token"))
        if request.method == "GET":
            reference = request.url.params["externalReference"]
            self.customer_lookups.append(reference)
            return self._page(
                [c for c in self.customers.values() if c["externalReference"] == reference]
            )
        body = json.loads(request.content)
        self.customer_posts.append(body)
        if self.reject_customers:
            return httpx.Response(
                400, json={"errors": [{"code": "invalid_cpfCnpj", "description": "CPF inválido"}]}
            )
        created = {
            "object": "customer",
            "id": f"cus_{len(self.customers) + 1:012d}",
            "name": body["name"],
            "externalReference": body["externalReference"],
        }
        failure, self.fail_next_customer_post = self.fail_next_customer_post, None
        if failure is not None:
            if self.store_customer_before_failing:
                self.customers[created["id"]] = created
            raise failure
        self.customers[created["id"]] = created
        return self._json(created)

    def _new(self, body: dict[str, Any]) -> dict[str, Any]:
        assert body["customer"] in self.customers, "a charge was sent without a linked customer"
        return {
            "object": "payment",
            "id": f"pay_{len(self.payments) + 1:06d}",
            "status": "PENDING",
            "value": body["value"],
            "netValue": body["value"],
            "billingType": body["billingType"],
            "customer": body["customer"],
            "externalReference": body["externalReference"],
            "deleted": False,
        }


async def with_document(s: Shop) -> None:
    async with s.api.client() as client:
        response = await client.put(
            f"/v1/projects/{s.project_id}/customers/buyer-1",
            json={
                "name": "Maria Silva",
                "kind": "individual",
                "document": TEST_CPF,
                "email": "maria@example.com",
                "phone": "+55 (11) 98765-4321",
            },
            headers=bearer(s.key),
        )
    assert response.status_code == 200, response.text
    assert TEST_CPF not in response.text


async def provision_row(engine: AsyncEngine, s: Shop) -> Any:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT status, failure_code FROM provider_customer_provisions "
                    "WHERE identity_id = :i"
                ),
                {"i": parse_id(s.customer_id, IdPrefix.CUSTOMER)},
            )
        ).one_or_none()


async def linked(engine: AsyncEngine, s: Shop) -> list[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT provider_customer_id FROM provider_customers WHERE identity_id = :i"),
            {"i": parse_id(s.customer_id, IdPrefix.CUSTOMER)},
        )
        return [row.provider_customer_id for row in rows.all()]


async def test_a_customer_is_created_once_with_its_document_and_then_reused(
    api: Api, app_engine: AsyncEngine
) -> None:
    asaas = CustomerAsaas()
    async with shop(api) as s:
        wire(api, asaas)
        account_id = await account_of(s)
        await with_document(s)

        with capture_logs() as logs:
            first = await s.create(provider_account_id=account_id)
            second = await s.create(provider_account_id=account_id)
        mapping = await linked(app_engine, s)
        provision = await provision_row(app_engine, s)

    assert first.status_code == second.status_code == 201, first.text
    assert first.json()["status"] == second.json()["status"] == "requires_payment"
    [posted] = asaas.customer_posts
    assert posted == {
        "name": "Maria Silva",
        "externalReference": s.customer_id,  # our cus_ id: what makes a lost answer resolvable
        "notificationDisabled": True,
        "email": "maria@example.com",
        "mobilePhone": "11987654321",
        "cpfCnpj": TEST_CPF,
    }
    [customer_id] = asaas.customers
    assert mapping == [customer_id]
    assert [p["customer"] for p in asaas.payments.values()] == [customer_id, customer_id]
    assert asaas.customer_lookups == [s.customer_id]  # the second payment used the mapping
    assert (provision.status, provision.failure_code) == ("created", None)
    assert TEST_CPF not in repr(logs)
    assert TEST_CPF not in first.text + second.text


async def test_a_lost_customer_answer_converges_without_a_second_customer(
    api: Api, app_engine: AsyncEngine
) -> None:
    asaas = CustomerAsaas()
    asaas.fail_next_customer_post = httpx.ReadTimeout("response lost")
    asaas.store_customer_before_failing = True  # Asaas created it; only the answer was lost
    async with shop(api) as s:
        registry = wire(api, asaas)
        account_id = await account_of(s)
        await with_document(s)

        view = (await s.create(provider_account_id=account_id)).json()
        unknown = await creation_status(app_engine, view["id"])
        waiting = await provision_row(app_engine, s)
        charges_before = asaas.post_count
        resolved = await service_for(s, registry).resolve_payment(intent_uuid(view["id"]))
        mapping = await linked(app_engine, s)
        provision = await provision_row(app_engine, s)

    assert view["status"] == "processing" and unknown == "unknown"
    assert waiting.status == "unknown"
    assert charges_before == 0  # no charge without a confirmed customer
    assert resolved is not None and resolved.value == "created"
    assert len(asaas.customer_posts) == 1 and len(asaas.customers) == 1  # found, not re-created
    assert mapping == list(asaas.customers)
    assert asaas.post_count == 1 and len(asaas.payments) == 1
    assert provision.status == "created"


async def test_a_refused_connection_is_provisioned_by_the_sweeper(
    api: Api, app_engine: AsyncEngine
) -> None:
    asaas = CustomerAsaas()
    asaas.fail_next_customer_post = httpx.ConnectError("no route")
    async with shop(api) as s:
        registry = wire(api, asaas)
        account_id = await account_of(s)
        await with_document(s)

        view = (await s.create(provider_account_id=account_id)).json()
        await processor_for(s, registry, sweeper_batch_size=1000).sweep()
        after = await creation_status(app_engine, view["id"])
        mine = [p for p in asaas.payments.values() if p["externalReference"] == view["id"]]

    assert view["status"] == "processing"
    assert after == "created"
    assert len(asaas.customer_posts) == 2 and len(asaas.customers) == 1
    assert len(mine) == 1


async def test_a_rejected_customer_rejects_the_payment_and_no_charge_is_sent(
    api: Api, app_engine: AsyncEngine
) -> None:
    asaas = CustomerAsaas()
    asaas.reject_customers = True
    async with shop(api) as s:
        wire(api, asaas)
        account_id = await account_of(s)

        with capture_logs() as logs:
            view = (await s.create(provider_account_id=account_id)).json()
        status = await creation_status(app_engine, view["id"])
        provision = await provision_row(app_engine, s)
        code = await scalar(
            app_engine,
            "SELECT failure_code FROM payment_intents WHERE id = :id",
            id=intent_uuid(view["id"]),
        )

    assert status == "rejected" and view["status"] == "failed"
    assert code == "customer_invalid_cpfCnpj"
    assert (provision.status, provision.failure_code) == ("rejected", "invalid_cpfCnpj")
    assert asaas.post_count == 0
    assert TEST_CPF not in repr(logs)


async def test_a_provisioning_call_in_flight_is_never_doubled(
    api: Api, app_engine: AsyncEngine
) -> None:
    asaas = CustomerAsaas()
    async with shop(api) as s:
        registry = wire(api, asaas)
        account_id = await account_of(s)
        now = api.clock.now()
        async with app_engine.begin() as conn:  # another worker holds the lease right now
            await conn.execute(
                text(
                    "INSERT INTO provider_customer_provisions (identity_id, provider_account_id, "
                    "project_id, attempt_id, status, lease_until, created_at, updated_at) "
                    "VALUES (:i, :a, :p, :t, 'requested', :until, :now, :now)"
                ),
                {
                    "i": parse_id(s.customer_id, IdPrefix.CUSTOMER),
                    "a": parse_id(account_id, IdPrefix.PROVIDER_ACCOUNT),
                    "p": s.project_uuid,
                    "t": new_uuid(),
                    "until": now + DEFAULT_LEASE,
                    "now": now,
                },
            )

        view = (await s.create(provider_account_id=account_id)).json()
        posts_while_leased = len(asaas.customer_posts)
        api.clock.advance(DEFAULT_LEASE + timedelta(seconds=1))  # that holder never came back
        resolved = await service_for(s, registry).resolve_payment(intent_uuid(view["id"]))

    assert view["status"] == "processing"
    assert posts_while_leased == 0
    assert resolved is not None and resolved.value == "created"
    assert len(asaas.customer_posts) == 1 and asaas.post_count == 1
