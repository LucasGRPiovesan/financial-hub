"""The payments API: create, reads, idempotency, validation, scopes and events (real Postgres)."""

from contextlib import aclosing

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from financial_hub.modules.identity.application.api import Role
from financial_hub.modules.payments.application.registry import ProviderRegistry
from financial_hub.modules.payments.infrastructure.fake_provider import FakeBehavior
from tests.integration.identity.support import Api, csrf, logged_in
from tests.integration.payments.helpers import events_of, intent_uuid, scalar, shop
from tests.integration.projects.helpers import create_account, create_key, member


async def test_create_returns_201_with_the_derived_status(
    api: Api, app_engine: AsyncEngine
) -> None:
    async with shop(api) as s:
        created = await s.create(external_ref="order-1", metadata={"order": "1"})
        payment = created.json()
        fetched = await s.get(f"/{payment['id']}")

    assert created.status_code == 201, created.text
    assert payment["id"].startswith("pi_")
    assert payment["status"] == "requires_payment"
    assert (payment["amount"], payment["currency"], payment["method"]) == (1000, "BRL", "pix")
    assert payment["customer_id"] == s.customer_id
    assert payment["provider"] == "fake" and payment["provider_account_id"] is None
    assert payment["provider_payment_id"] == f"fake_pay_{s.fake.namespace}_000001"
    assert payment["capture_status"] == "pending"
    assert (payment["captured_amount"], payment["refundable_amount"]) == (0, 0)
    assert payment["metadata"] == {"order": "1"}
    assert fetched.json() == payment
    assert type(payment["amount"]) is int
    events = await events_of(app_engine, s.project_uuid, payment["id"])
    assert events[0] == (
        "payment.created",
        {"payment_id": payment["id"], "customer_id": s.customer_id, "status": "processing"},
    )
    assert events[1] == (
        "payment.status_changed",
        {
            "payment_id": payment["id"],
            "previous_status": "processing",
            "status": "requires_payment",
        },
    )
    attempts = await scalar(
        app_engine,
        "SELECT count(*) FROM payment_attempts "
        "WHERE payment_intent_id = :id AND status = 'succeeded'",
        id=intent_uuid(payment["id"]),
    )
    assert attempts == 1


async def test_customer_by_external_ref_and_a_captured_create(api: Api) -> None:
    async with shop(api) as s:
        s.fake.script(FakeBehavior.SUCCEED_CAPTURED)
        created = await s.create(customer={"external_ref": "buyer-1"}, method="card")

    assert created.status_code == 201, created.text
    assert created.json()["status"] == "succeeded"
    assert created.json()["captured_amount"] == created.json()["refundable_amount"] == 1000


async def test_a_rejected_create_is_failed_with_its_code(api: Api) -> None:
    async with shop(api) as s:
        s.fake.script(FakeBehavior.REJECT)
        created = await s.create()

    assert created.status_code == 201
    assert created.json()["status"] == "failed"
    assert created.json()["failure_code"] == "invalid_request"
    assert created.json()["provider_payment_id"] is None


async def test_list_is_newest_first_with_cursor_pagination(api: Api) -> None:
    async with shop(api) as s:
        ids = [(await s.create()).json()["id"] for _ in range(3)]
        first = await s.get("", limit=2)
        second = await s.get("", limit=2, cursor=first.json()["next_cursor"])

    assert [row["id"] for row in first.json()["data"]] == [ids[2], ids[1]]
    assert first.json()["has_more"] is True
    assert [row["id"] for row in second.json()["data"]] == [ids[0]]
    assert second.json()["has_more"] is False


async def test_idempotent_create_replays_and_refuses_a_different_body(
    api: Api, app_engine: AsyncEngine
) -> None:
    async with shop(api) as s:
        first = await s.create(idempotency_key="pay-key-1")
        again = await s.create(idempotency_key="pay-key-1")
        different = await s.create(idempotency_key="pay-key-1", amount=2000)
        async with api.client() as client:
            missing = await client.post(
                s.base, json=s.body(), headers={"Authorization": f"Bearer {s.key}"}
            )
        count = await scalar(
            app_engine,
            "SELECT count(*) FROM payment_intents WHERE project_id = :p",
            p=s.project_uuid,
        )

    assert first.status_code == again.status_code == 201
    assert again.json() == first.json()
    assert different.status_code in {409, 422}
    assert missing.status_code == 400
    assert count == 1
    assert s.fake.payment_count == 1


async def test_validation_errors_point_at_the_field(api: Api) -> None:
    async with shop(api) as s:
        float_amount = await s.create(amount=10.5)
        string_amount = await s.create(amount="1000")
        zero = await s.create(amount=0)
        bad_method = await s.create(method="cash")
        boleto_usd = await s.create(method="boleto", currency="USD")
        bad_currency = await s.create(currency="XXZ")
        past = await s.create(due_date="2020-01-01")
        bad_ref = await s.create(external_ref="has space")
        no_customer = await s.create(customer={})
        unknown_customer = await s.create(customer={"id": "cus_00000000000000000000000000"})
        unknown_ref = await s.create(customer={"external_ref": "nobody"})
        bad_account = await s.create(provider_account_id="not-an-id")
        blank_description = await s.create(description="   ")
        big_metadata = await s.create(metadata={"x": "y" * 9000})

    def pointer(response: httpx.Response) -> str:
        return response.json()["errors"][0]["pointer"]

    for response in (float_amount, string_amount, zero):
        assert response.status_code == 422
    assert (bad_method.status_code, pointer(bad_method)) == (422, "/method")
    assert pointer(boleto_usd) == "/currency"
    assert pointer(bad_currency) == "/currency"
    assert pointer(past) == "/due_date"
    assert pointer(bad_ref) == "/external_ref"
    assert pointer(no_customer) == "/customer"
    assert pointer(unknown_customer) == "/customer"
    assert pointer(unknown_ref) == "/customer"
    assert pointer(bad_account) == "/provider_account_id"
    assert pointer(blank_description) == "/description"
    assert pointer(big_metadata) == "/metadata"
    assert s.fake.payment_count == 0


async def test_an_external_ref_is_unique_per_project(api: Api) -> None:
    async with shop(api) as s:
        first = await s.create(external_ref="order-9")
        taken = await s.create(external_ref="order-9")
    async with shop(api) as other:
        elsewhere = await other.create(external_ref="order-9")

    assert first.status_code == 201
    assert taken.status_code == 409
    assert taken.json()["type"].endswith("payment-external-ref-taken")
    assert elsewhere.status_code == 201


async def test_unknown_and_foreign_payment_ids_are_404(api: Api) -> None:
    async with shop(api) as a, shop(api) as b:
        mine = (await a.create()).json()["id"]
        foreign = await b.get(f"/{mine}")
        malformed = await a.get("/nope")
        wrong_prefix = await a.get(f"/{mine.replace('pi_', 'cus_')}")
        refund_foreign = await b.refund(mine)

    assert (foreign.status_code, malformed.status_code, wrong_prefix.status_code) == (404, 404, 404)
    assert refund_foreign.status_code == 404


async def test_the_fake_provider_is_refused_when_disabled(api: Api) -> None:
    async with shop(api) as s:
        api.app.state.payment_providers = ProviderRegistry()
        refused = await s.create()

    assert refused.status_code == 422
    assert refused.json()["errors"][0]["pointer"] == "/provider_account_id"


async def test_a_real_account_is_chosen_but_needs_an_adapter(api: Api) -> None:
    async with shop(api) as s:
        client, token = await logged_in(api, s.owner)
        async with aclosing(client):
            account = await create_account(client, token, s.project_id)
            only = await s.create()
            named = await s.create(provider_account_id=account["id"])
            await create_account(client, token, s.project_id, name="second")
            ambiguous = await s.create()
            missing = await s.create(provider_account_id="pva_00000000000000000000000000")

    # the asaas adapter arrives with PRO-41: until then the account cannot take payments
    for response in (only, named, missing, ambiguous):
        assert response.status_code == 422, response.text
        assert response.json()["errors"][0]["pointer"] == "/provider_account_id"
    assert only.json()["errors"][0]["code"] == "provider_not_supported"
    assert ambiguous.json()["errors"][0]["code"] == "required"
    assert missing.json()["errors"][0]["code"] == "not_found"
    assert s.fake.payment_count == 0


async def test_key_scopes_and_roles(api: Api) -> None:
    async with shop(api, scopes=("payments:read",)) as s:
        client, token = await logged_in(api, s.owner)
        async with aclosing(client):
            write_key = (await create_key(client, token, s.project_id, scopes=["payments:write"]))[
                "key"
            ]
            refund_key = (
                await create_key(client, token, s.project_id, scopes=["payments:refund"])
            )["key"]
            customers_only = (
                await create_key(client, token, s.project_id, scopes=["customers:read"])
            )["key"]
        denied = await s.create()
        created = await s.create(key=write_key)
        payment_id = created.json()["id"]
        read = await s.get(f"/{payment_id}")
        listed = await s.get("")
        no_read = await s.get("", key=customers_only)
        refund_denied = await s.refund(payment_id)
        refund_with_write = await s.refund(payment_id, key=write_key)
        refund_with_refund = await s.refund(payment_id, key=refund_key)
        create_with_refund = await s.create(key=refund_key)
        _viewer, viewer_client, viewer_csrf = await member(api, s.project_id, Role.VIEWER)
        async with aclosing(viewer_client):
            viewer_create = await viewer_client.post(
                s.base, json=s.body(), headers={**csrf(viewer_csrf), "Idempotency-Key": "v-1"}
            )
            viewer_read = await viewer_client.get(f"{s.base}/{payment_id}")
        _admin, admin_client, admin_csrf = await member(api, s.project_id, Role.ADMIN)
        async with aclosing(admin_client):
            admin_create = await admin_client.post(
                s.base, json=s.body(), headers={**csrf(admin_csrf), "Idempotency-Key": "a-1"}
            )
        _finance, finance_client, finance_csrf = await member(api, s.project_id, Role.FINANCE)
        async with aclosing(finance_client):
            finance_create = await finance_client.post(
                s.base, json=s.body(), headers={**csrf(finance_csrf), "Idempotency-Key": "f-1"}
            )

    assert denied.status_code == 403
    assert created.status_code == 201
    assert read.status_code == listed.status_code == 200
    assert no_read.status_code == 403
    assert refund_denied.status_code == 403
    # refunds need their own scope: payments:write cannot refund, payments:refund cannot create
    assert refund_with_write.status_code == 403
    assert refund_with_refund.status_code == 409  # allowed, but this payment is not captured
    assert create_with_refund.status_code == 403
    assert viewer_create.status_code == 403 and viewer_read.status_code == 200
    assert admin_create.status_code == 403  # money movement is finance/owner only
    assert finance_create.status_code == 201, finance_create.text
