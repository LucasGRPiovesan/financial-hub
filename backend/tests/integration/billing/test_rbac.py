"""Who may bill (SECURITY.md, docs/DOMAIN.md "Billing"): separation of duties on money movement.

``billing:manage`` is money movement: owner and finance only. Admin and viewer read. The catalog
(prices, plans) and invoice changes (finalize, void) are operator-only; an API key holding
``billing:write`` may manage subscriptions, ``billing:read`` only reads.
"""

import secrets
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any

import httpx

from financial_hub.modules.identity.application.api import Role
from financial_hub.shared.ids import IdPrefix, format_id
from tests.integration.billing.helpers import FEE_PRICE, USAGE_ONLY_PLAN, Billing, billing, utc
from tests.integration.identity.support import Api, csrf, logged_in
from tests.integration.projects.helpers import bearer, create_key, member

PRICE = {
    "code": "setup",
    "name": "Setup",
    "currency": "BRL",
    "model": {"kind": "flat_fee", "amount": 1000},
}


@dataclass(frozen=True)
class Targets:
    customer_id: str
    subscription_id: str
    invoice_id: str


async def with_invoice(b: Billing) -> Targets:
    """A subscription whose March invoice is finalized."""
    subscription = await b.subscribe()
    (invoice_id,) = await b.close(subscription, utc(4, 1, 1))
    return Targets(
        customer_id=b.shop.customer_id,
        subscription_id=format_id(IdPrefix.SUBSCRIPTION, subscription.subscription.id),
        invoice_id=format_id(IdPrefix.INVOICE, invoice_id),
    )


async def writes(
    client: httpx.AsyncClient, base: str, headers: dict[str, str], targets: Targets
) -> dict[str, int]:
    plan = {"code": f"p-{secrets.token_hex(3)}", "name": "P", "prices": [FEE_PRICE]}
    subscription = {"customer_id": targets.customer_id, "plan": USAGE_ONLY_PLAN}
    calls: dict[str, tuple[str, dict[str, Any]]] = {
        "price": ("/prices", PRICE),
        "plan": ("/plans", plan),
        "subscription": ("/subscriptions", subscription),
        "cancel": (f"/subscriptions/{targets.subscription_id}/cancel", {"at_period_end": True}),
        "finalize": (f"/invoices/{targets.invoice_id}/finalize", {}),
        "void": (f"/invoices/{targets.invoice_id}/void", {}),
    }
    statuses: dict[str, int] = {}
    for name, (path, body) in calls.items():
        response = await client.post(
            f"{base}{path}",
            json=body,
            headers={**headers, "Idempotency-Key": secrets.token_hex(8)},
        )
        statuses[name] = response.status_code
    return statuses


async def reads(
    client: httpx.AsyncClient, base: str, headers: dict[str, str], targets: Targets
) -> dict[str, int]:
    paths = (
        "/prices",
        "/plans",
        "/subscriptions",
        f"/subscriptions?customer_id={targets.customer_id}",
        f"/subscriptions/{targets.subscription_id}",
        "/invoices",
        f"/invoices?subscription_id={targets.subscription_id}",
        f"/invoices/{targets.invoice_id}",
    )
    return {
        path: (await client.get(f"{base}{path}", headers=headers)).status_code for path in paths
    }


async def test_admin_and_viewer_read_but_never_move_money(api: Api) -> None:
    async with billing(api) as b:
        targets = await with_invoice(b)
        for role in (Role.ADMIN, Role.VIEWER):
            _, client, token = await member(api, b.shop.project_id, role)
            async with aclosing(client):
                read = await reads(client, b.base, {}, targets)
                denied = await writes(client, b.base, csrf(token), targets)
            assert set(read.values()) == {200}, (role, read)
            assert set(denied.values()) == {403}, (role, denied)


async def test_finance_manages_billing(api: Api) -> None:
    async with billing(api) as b:
        targets = await with_invoice(b)
        _, client, token = await member(api, b.shop.project_id, Role.FINANCE)
        async with aclosing(client):
            plan = await client.post(
                f"{b.base}/plans",
                json={"code": "finance-plan", "name": "Finance", "prices": [FEE_PRICE]},
                headers=csrf(token),
            )
            invoices = await client.get(
                f"{b.base}/invoices", params={"subscription_id": targets.subscription_id}
            )
            voided = await client.post(
                f"{b.base}/invoices/{targets.invoice_id}/void", headers=csrf(token)
            )
            canceled = await client.post(
                f"{b.base}/subscriptions/{targets.subscription_id}/cancel",
                json={"at_period_end": True},
                headers=csrf(token),
            )
            subscriptions = await client.get(
                f"{b.base}/subscriptions", params={"customer_id": targets.customer_id}
            )
        assert plan.status_code == 201, plan.text
        assert [invoice["id"] for invoice in invoices.json()["data"]] == [targets.invoice_id]
        assert voided.status_code == 200, voided.text
        assert voided.json()["status"] == "void"
        assert canceled.status_code == 200, canceled.text
        assert [s["id"] for s in subscriptions.json()["data"]] == [targets.subscription_id]


async def test_api_keys_manage_subscriptions_but_never_the_catalog_or_invoices(api: Api) -> None:
    async with billing(api) as b:
        targets = await with_invoice(b)
        client, token = await logged_in(api, b.shop.owner)
        async with aclosing(client):
            reader = await create_key(
                client, token, b.shop.project_id, scopes=["billing:read"], name="reader"
            )
            outsider = await create_key(
                client, token, b.shop.project_id, scopes=["payments:read"], name="outsider"
            )
        async with api.client() as keys:
            writer = await writes(keys, b.base, bearer(b.shop.key), targets)  # billing:write
            reader_reads = await reads(keys, b.base, bearer(reader["key"]), targets)
            reader_writes = await writes(keys, b.base, bearer(reader["key"]), targets)
            outsider_reads = await reads(keys, b.base, bearer(outsider["key"]), targets)

        assert (writer["subscription"], writer["cancel"]) == (201, 200), writer
        assert {writer[name] for name in ("price", "plan", "finalize", "void")} == {403}, writer
        assert set(reader_reads.values()) == {200}, reader_reads
        assert set(reader_writes.values()) == {403}, reader_writes
        assert set(outsider_reads.values()) == {403}, outsider_reads
