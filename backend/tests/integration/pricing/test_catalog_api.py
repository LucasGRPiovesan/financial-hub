"""The price catalog over HTTP (docs/DOMAIN.md "Billing"): versioned prices, operator-only changes.

Real Postgres as ``fh_app``; the clock is pinned at ``T0`` (2026-03-01), so "the future" is any
instant after it. Prices are append-only: a new version ends the open one, history never moves.
"""

import secrets
from contextlib import aclosing
from typing import Any

from financial_hub.modules.identity.application.api import Role
from tests.integration.billing.helpers import CALLS_PRICE, FEE_PRICE, METRIC, billing
from tests.integration.identity.support import Api, csrf, logged_in
from tests.integration.projects.helpers import bearer, member

STORAGE = {
    "code": "storage",
    "name": "Storage",
    "currency": "BRL",
    "metric_code": METRIC,
    "model": {"kind": "per_unit", "unit_price": "0.02"},
    "effective_from": "2026-02-01T00:00:00Z",
}
LATER = {
    "model": {"kind": "per_unit", "unit_price": "0.03"},
    "effective_from": "2026-04-01T00:00:00Z",
}


def idempotent(headers: dict[str, str]) -> dict[str, str]:
    return {**headers, "Idempotency-Key": secrets.token_hex(8)}


async def test_owner_creates_lists_and_versions_a_price(api: Api) -> None:
    async with billing(api) as b:
        client, token = await logged_in(api, b.shop.owner)
        async with aclosing(client):
            created = await client.post(f"{b.base}/prices", json=STORAGE, headers=csrf(token))
            taken = await client.post(f"{b.base}/prices", json=STORAGE, headers=csrf(token))
            versioned = await client.post(
                f"{b.base}/prices/storage/versions", json=LATER, headers=csrf(token)
            )
            again = await client.post(
                f"{b.base}/prices/storage/versions",
                json={**LATER, "effective_from": "2026-03-15T00:00:00Z"},
                headers=csrf(token),
            )
            past = await client.post(
                f"{b.base}/prices/{FEE_PRICE}/versions",
                json={
                    "model": {"kind": "flat_fee", "amount": 5990},
                    "effective_from": "2026-02-01T00:00:00Z",
                },
                headers=csrf(token),
            )
            fetched = await client.get(f"{b.base}/prices/storage")
            listed = await client.get(f"{b.base}/prices")
            missing = await client.get(f"{b.base}/prices/nope")

        assert created.status_code == 201, created.text
        body = created.json()
        assert (body["code"], body["kind"], body["metric_code"]) == ("storage", "usage", METRIC)
        assert body["versions"] == [
            {
                "version": 1,
                "model": {"kind": "per_unit", "unit_price": "0.02"},
                "effective_from": "2026-02-01T00:00:00Z",
                "effective_to": None,
            }
        ]
        assert taken.status_code == 409, taken.text
        assert versioned.status_code == 201, versioned.text
        first, second = versioned.json()["versions"]
        assert first["effective_to"] == "2026-04-01T00:00:00Z"  # the open version ends there
        assert (second["version"], second["model"]["unit_price"]) == (2, "0.03")
        assert second["effective_to"] is None
        assert again.status_code == 409, again.text  # a later version is already scheduled
        assert past.status_code == 422, past.text  # history is never rewritten
        assert fetched.json() == versioned.json()
        assert {price["code"] for price in listed.json()["data"]} == {
            FEE_PRICE,
            CALLS_PRICE,
            "storage",
        }
        assert missing.status_code == 404


async def test_a_price_is_validated_against_its_metric_and_model(api: Api) -> None:
    async with billing(api) as b:
        client, token = await logged_in(api, b.shop.owner)
        invalid: dict[str, dict[str, Any]] = {
            "usage without metric": {**STORAGE, "code": "a", "metric_code": None},
            "fixed with metric": {
                **STORAGE,
                "code": "b",
                "model": {"kind": "flat_fee", "amount": 100},
            },
            "unknown metric": {**STORAGE, "code": "c", "metric_code": "nope"},
            "float amount": {**STORAGE, "code": "d", "model": {"kind": "flat_fee", "amount": 1.5}},
            "unknown currency": {**STORAGE, "code": "e", "currency": "XXZ"},
            "naive start": {**STORAGE, "code": "f", "effective_from": "2026-02-01T00:00:00"},
        }
        async with aclosing(client):
            statuses = {
                case: (
                    await client.post(f"{b.base}/prices", json=body, headers=csrf(token))
                ).status_code
                for case, body in invalid.items()
            }
        assert set(statuses.values()) == {422}, statuses


async def test_only_operators_with_billing_manage_change_the_catalog(api: Api) -> None:
    async with billing(api) as b:
        statuses: dict[str, tuple[int, int, int]] = {}
        for role in (Role.ADMIN, Role.VIEWER, Role.FINANCE):
            _, client, token = await member(api, b.shop.project_id, role)
            async with aclosing(client):
                read = await client.get(f"{b.base}/prices/{FEE_PRICE}")
                create = await client.post(
                    f"{b.base}/prices",
                    json={**STORAGE, "code": f"by-{role.value}"},
                    headers=csrf(token),
                )
                version = await client.post(
                    f"{b.base}/prices/{CALLS_PRICE}/versions",
                    json={**LATER, "effective_from": f"2026-0{4 + len(statuses)}-01T00:00:00Z"},
                    headers=csrf(token),
                )
            statuses[role.value] = (read.status_code, create.status_code, version.status_code)
        async with api.client() as keys:  # billing:read + billing:write, never the catalog
            key_read = await keys.get(f"{b.base}/prices", headers=bearer(b.shop.key))
            key_create = await keys.post(
                f"{b.base}/prices",
                json={**STORAGE, "code": "by-key"},
                headers=idempotent(bearer(b.shop.key)),
            )
            key_version = await keys.post(
                f"{b.base}/prices/{CALLS_PRICE}/versions",
                json=LATER,
                headers=idempotent(bearer(b.shop.key)),
            )

        assert statuses == {
            Role.ADMIN.value: (200, 403, 403),
            Role.VIEWER.value: (200, 403, 403),
            Role.FINANCE.value: (200, 201, 201),
        }
        assert (key_read.status_code, key_create.status_code, key_version.status_code) == (
            200,
            403,
            403,
        )
