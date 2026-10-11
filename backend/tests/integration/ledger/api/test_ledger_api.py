"""``/v1/projects/{id}/ledger`` read API against real Postgres (as ``fh_app``), PRO-36.

The books are seeded with real recipes (R1->R2/R3->R4 golden flow, an R9 dispute opened and won,
an R8 refund left pending), so every figure comes from the ledger itself.
"""

from contextlib import aclosing
from typing import Any

import httpx

from financial_hub.modules.identity.application.api import Role
from financial_hub.modules.ledger.application import (
    PAYEE_PAYABLE_AVAILABLE,
    PROVIDER_BALANCE,
    PROVIDER_FEES,
)
from tests.integration.identity.support import Api, logged_in
from tests.integration.ledger.api.seed import (
    CORRELATION,
    PAYEE,
    PROVIDER_ACCOUNT,
    T1,
    T2,
    T3,
    T4,
    T5,
    Seeded,
    complete_refund,
    customer_id,
    payee_id,
    provider_account_id,
)
from tests.integration.projects.helpers import member

Client = httpx.AsyncClient


async def get(client: Client, url: str, **params: Any) -> httpx.Response:
    return await client.get(url, params=params)


async def pages(client: Client, url: str, limit: int, **params: Any) -> list[list[dict[str, Any]]]:
    """Follow ``next_cursor`` to the end; every page is a list of rows."""
    out: list[list[dict[str, Any]]] = []
    cursor: str | None = None
    while True:
        extra = {"cursor": cursor} if cursor else {}
        response = await get(client, url, limit=limit, **params, **extra)
        assert response.status_code == 200, response.text
        body = response.json()
        out.append(body["data"])
        assert body["has_more"] == (body["next_cursor"] is not None)
        if not body["has_more"]:
            return out
        cursor = body["next_cursor"]


async def account(client: Client, seeded: Seeded, code: str) -> dict[str, Any]:
    response = await get(client, f"{seeded.base}/accounts", code=code)
    assert response.status_code == 200, response.text
    (found,) = response.json()["data"]
    return found


def minor(money: dict[str, Any]) -> int:
    assert set(money) == {"amount_minor", "currency"}
    assert type(money["amount_minor"]) is int
    return money["amount_minor"]


# --- accounts -------------------------------------------------------------------------------------


async def test_accounts_pagination_is_complete_and_stable(client: Client, seeded: Seeded) -> None:
    url = f"{seeded.base}/accounts"
    everything = (await get(client, url, limit=200)).json()["data"]
    assert len(everything) >= 8
    assert all(row["id"].startswith("acc_") for row in everything)

    paged = await pages(client, url, limit=3)
    flat = [row for page in paged for row in page]
    assert [row["id"] for row in flat] == [row["id"] for row in everything]
    assert len({row["id"] for row in flat}) == len(flat)
    assert all(len(page) == 3 for page in paged[:-1])
    again = [row["id"] for page in await pages(client, url, limit=3) for row in page]
    assert again == [row["id"] for row in flat]


async def test_accounts_filters(client: Client, seeded: Seeded) -> None:
    url = f"{seeded.base}/accounts"
    everything = (await get(client, url, limit=200)).json()["data"]

    by_code = (await get(client, url, code=PROVIDER_BALANCE.code(PROVIDER_ACCOUNT))).json()["data"]
    assert [row["code"] for row in by_code] == [PROVIDER_BALANCE.code(PROVIDER_ACCOUNT)]
    assert by_code[0]["provider_account_id"] == provider_account_id()
    assert by_code[0]["type"] == "asset" and by_code[0]["normal_balance"] == "debit"

    by_type = (await get(client, url, type="income", limit=200)).json()["data"]
    assert by_type and {row["type"] for row in by_type} == {"income"}
    assert len(by_type) < len(everything)

    for owner, field in (
        (customer_id(), "customer_id"),
        (payee_id(), "payee_id"),
        (provider_account_id(), "provider_account_id"),
    ):
        owned = (await get(client, url, owner_id=owner, limit=200)).json()["data"]
        assert owned and all(row[field] == owner for row in owned)

    assert (await get(client, url, currency="BRL", limit=200)).json()["data"] == everything
    assert (await get(client, url, currency="USD")).json()["data"] == []


async def test_accounts_reject_bad_filters(client: Client, seeded: Seeded) -> None:
    url = f"{seeded.base}/accounts"
    for params in (
        {"currency": "XXZ"},
        {"owner_id": "inv_01h455vb4pex5vsknk084sn02q"},
        {"owner_id": "cus_nope"},
        {"cursor": "not-a-cursor"},
        {"limit": 0},
        {"limit": 201},
        {"type": "nonsense"},
    ):
        response = await get(client, url, **params)
        assert response.status_code == 422, params
        assert response.headers["content-type"].startswith("application/problem+json")


async def test_account_detail_with_current_balance(client: Client, seeded: Seeded) -> None:
    listed = await account(client, seeded, PAYEE_PAYABLE_AVAILABLE.code(PAYEE))
    response = await get(client, f"{seeded.base}/accounts/{listed['id']}")
    assert response.status_code == 200
    detail = response.json()
    assert detail == listed
    balance = detail["balance"]
    assert detail["currency"] == "BRL" and detail["no_overdraft"] is True
    # The pending R8 refund holds the payee's whole share: posted stays, available drops to zero.
    assert minor(balance["posted"]) == 14851
    assert minor(balance["available"]) == 0
    assert minor(balance["pending"]) == -14851
    assert balance["posted"]["currency"] == "BRL"


# --- balance as of --------------------------------------------------------------------------------


async def test_balance_as_of_before_and_after_an_entry(client: Client, seeded: Seeded) -> None:
    provider = await account(client, seeded, PROVIDER_BALANCE.code(PROVIDER_ACCOUNT))
    url = f"{seeded.base}/accounts/{provider['id']}/balance"

    async def posted(as_of: str) -> dict[str, Any]:
        response = await get(client, url, as_of=as_of)
        assert response.status_code == 200, response.text
        return response.json()

    assert minor((await posted(T1.isoformat()))["posted"]) == 0
    assert minor((await posted(T2.isoformat()))["posted"]) == 0  # R4 settles the day after
    after = await posted(T3.isoformat())  # effective_at == as_of counts (inclusive)
    assert minor(after["posted"]) == 19801
    assert minor(after["debits"]) == 19801 and minor(after["credits"]) == 0
    assert after["account_id"] == provider["id"] and after["currency"] == "BRL"
    assert minor((await posted("2030-01-01T00:00:00+00:00"))["posted"]) == 19801
    # An offset other than UTC is the same instant.
    assert minor((await posted("2026-03-03T07:00:00-03:00"))["posted"]) == 19801

    # Open (pending) refund: current available moves, the as-of posted balance does not.
    payable = await account(client, seeded, PAYEE_PAYABLE_AVAILABLE.code(PAYEE))
    far = await get(
        client, f"{seeded.base}/accounts/{payable['id']}/balance", as_of="2030-01-01T00:00:00Z"
    )
    assert minor(far.json()["posted"]) == 14851


async def test_balance_as_of_recomputes_the_cached_posted_balance(
    api: Api, client: Client, seeded: Seeded
) -> None:
    await complete_refund(api, seeded)  # the pending refund is posted now (effective T5)
    fees = await account(client, seeded, PROVIDER_FEES.code())
    url = f"{seeded.base}/accounts/{fees['id']}/balance"
    before = await get(client, url, as_of=T4.isoformat())
    after = await get(client, url, as_of=T5.isoformat())
    assert minor(before.json()["posted"]) == 199  # the capture fee, booked at T2
    current = await get(client, f"{seeded.base}/accounts/{fees['id']}")
    assert minor(after.json()["posted"]) == minor(current.json()["balance"]["posted"])


async def test_balance_as_of_validation(client: Client, seeded: Seeded) -> None:
    provider = await account(client, seeded, PROVIDER_BALANCE.code(PROVIDER_ACCOUNT))
    url = f"{seeded.base}/accounts/{provider['id']}/balance"
    assert (await get(client, url)).status_code == 422
    assert (await get(client, url, as_of="yesterday")).status_code == 422
    naive = await get(client, url, as_of="2026-03-03T10:00:00")
    assert naive.status_code == 422
    assert naive.json()["errors"][0]["pointer"] == "/as_of"


# --- entries --------------------------------------------------------------------------------------


async def test_entries_pagination_is_complete_and_stable(client: Client, seeded: Seeded) -> None:
    url = f"{seeded.base}/entries"
    everything = (await get(client, url, limit=200)).json()["data"]
    assert sorted(e["recipe"] for e in everything).count("R9") == 2  # opened + reversal
    assert all(e["id"].startswith("je_") for e in everything)

    flat = [row for page in await pages(client, url, limit=2) for row in page]
    assert [e["id"] for e in flat] == [e["id"] for e in everything]
    assert len({e["id"] for e in flat}) == len(flat)
    # Newest first.
    created = [e["created_at"] for e in everything]
    assert created == sorted(created, reverse=True)


async def test_entries_filters(client: Client, seeded: Seeded) -> None:
    url = f"{seeded.base}/entries"
    every = (await get(client, url, limit=200)).json()["data"]

    r8 = (await get(client, url, recipe="R8")).json()["data"]
    assert [e["status"] for e in r8] == ["pending"]
    pending = (await get(client, url, status="pending")).json()["data"]
    assert [e["id"] for e in pending] == [r8[0]["id"]]
    posted = (await get(client, url, status="posted", limit=200)).json()["data"]
    assert len(posted) == len(every) - 1

    correlated = (await get(client, url, correlation_id=str(CORRELATION), limit=200)).json()["data"]
    assert len(correlated) == len(every)
    assert (await get(client, url, correlation_id="00000000-0000-0000-0000-000000000001")).json()[
        "data"
    ] == []

    payable = await account(client, seeded, PAYEE_PAYABLE_AVAILABLE.code(PAYEE))
    touching = (await get(client, url, account_id=payable["id"], limit=200)).json()["data"]
    assert {e["recipe"] for e in touching} >= {"R4", "R8"}
    assert len(touching) < len(every)

    window = await get(
        client, url, effective_from=T2.isoformat(), effective_to=T3.isoformat(), limit=200
    )
    assert {e["recipe"] for e in window.json()["data"]} == {"R2", "R3", "R4"}
    only_t1 = await get(client, url, effective_to=T1.isoformat(), limit=200)
    assert [e["recipe"] for e in only_t1.json()["data"]] == ["R1"]
    after_t5 = await get(client, url, effective_from=T5.isoformat(), limit=200)
    assert [e["recipe"] for e in after_t5.json()["data"]] == ["R8"]


async def test_entries_reject_bad_filters(client: Client, seeded: Seeded) -> None:
    url = f"{seeded.base}/entries"
    for params in (
        {"account_id": "je_01h455vb4pex5vsknk084sn02q"},
        {"correlation_id": "nope"},
        {"effective_from": "2026-03-01T00:00:00"},
        {"status": "nonsense"},
    ):
        assert (await get(client, url, **params)).status_code == 422, params


async def test_entry_detail_with_postings_and_reversal_link(client: Client, seeded: Seeded) -> None:
    url = f"{seeded.base}/entries"
    r9 = (await get(client, url, recipe="R9", limit=200)).json()["data"]
    opened = next(e for e in r9 if e["reverses_entry_id"] is None)
    reversal = next(e for e in r9 if e["reverses_entry_id"] is not None)
    assert reversal["reverses_entry_id"] == opened["id"]
    assert opened["reversed_by_entry_id"] == reversal["id"]
    assert reversal["reversed_by_entry_id"] is None

    response = await get(client, f"{url}/{opened['id']}")
    assert response.status_code == 200
    detail = response.json()
    assert {k: detail[k] for k in opened} == opened
    assert detail["reversed_by_entry_id"] == reversal["id"]
    assert detail["correlation_id"] == str(CORRELATION)
    assert detail["causation_id"] is None
    assert detail["idempotency_key"] and detail["status"] == "posted"
    assert isinstance(detail["metadata"], dict)

    postings = detail["postings"]
    assert len(postings) >= 2
    assert all(p["id"].startswith("pst_") and p["account_id"].startswith("acc_") for p in postings)
    assert all(p["account_code"] and p["direction"] in ("debit", "credit") for p in postings)
    debits = sum(minor(p["amount"]) for p in postings if p["direction"] == "debit")
    credits = sum(minor(p["amount"]) for p in postings if p["direction"] == "credit")
    assert debits == credits > 0

    drill = (await get(client, f"{url}/{reversal['id']}")).json()
    assert drill["reverses_entry_id"] == opened["id"]


# --- trial balance --------------------------------------------------------------------------------


def total(body: dict[str, Any], currency: str = "BRL") -> dict[str, Any]:
    (found,) = [t for t in body["totals"] if t["currency"] == currency]
    return found


async def test_trial_balance_nets_to_zero_after_a_mixed_flow(
    api: Api, client: Client, seeded: Seeded
) -> None:
    url = f"{seeded.base}/trial-balance"
    for stage in ("refund pending", "refund done"):
        if stage == "refund done":
            await complete_refund(api, seeded)
        response = await get(client, url)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["balanced"] is True and body["as_of"] is None, stage
        assert total(body)["debits"] == total(body)["credits"] > 0, stage
        assert total(body)["balanced"] is True
        assert sum(line["debits"] for line in body["lines"]) == total(body)["debits"]
        assert sum(line["credits"] for line in body["lines"]) == total(body)["credits"]
        assert {line["type"] for line in body["lines"]} >= {"asset", "liability", "income"}
        assert "customer_receivable" in {line["code"] for line in body["lines"]}
        assert all(type(line["debits"]) is int for line in body["lines"])

        # The recomputation from postings agrees with the materialized balances.
        recomputed = (await get(client, url, as_of="2030-01-01T00:00:00Z")).json()
        assert recomputed["balanced"] is True
        assert recomputed["totals"] == body["totals"]
        assert recomputed["lines"] == body["lines"]


async def test_trial_balance_as_of_and_currency(client: Client, seeded: Seeded) -> None:
    url = f"{seeded.base}/trial-balance"
    early = (await get(client, url, as_of=T1.isoformat())).json()
    assert early["balanced"] is True and early["as_of"] is not None
    assert {line["code"] for line in early["lines"]} == {"customer_receivable", "sales_clearing"}
    later = (await get(client, url, as_of=T3.isoformat())).json()
    assert total(later)["debits"] > total(early)["debits"]
    assert later["balanced"] is True

    brl = (await get(client, url, currency="BRL")).json()
    assert [t["currency"] for t in brl["totals"]] == ["BRL"]
    usd = (await get(client, url, currency="USD")).json()
    assert usd == {"as_of": None, "balanced": True, "totals": [], "lines": []}
    assert (await get(client, url, currency="nope")).status_code == 422
    assert (await get(client, url, as_of="2026-03-03T10:00:00")).status_code == 422


# --- isolation and access -------------------------------------------------------------------------


async def test_other_projects_rows_are_not_found(
    client: Client, other_client: Client, seeded: Seeded, other: Seeded
) -> None:
    theirs_account = await account(other_client, other, PROVIDER_BALANCE.code(PROVIDER_ACCOUNT))
    theirs_entry = (await get(other_client, f"{other.base}/entries", limit=1)).json()["data"][0]

    mine = f"{seeded.base}"
    for url in (
        f"{mine}/accounts/{theirs_account['id']}",
        f"{mine}/accounts/{theirs_account['id']}/balance?as_of=2030-01-01T00:00:00Z",
        f"{mine}/entries/{theirs_entry['id']}",
        f"{mine}/accounts/acc_not-an-id",
        f"{mine}/entries/je_not-an-id",
        f"{mine}/entries/acc_{theirs_entry['id'][3:]}",
    ):
        response = await client.get(url)
        assert response.status_code == 404, url

    mine_ids = {e["id"] for e in (await get(client, f"{mine}/entries", limit=200)).json()["data"]}
    assert theirs_entry["id"] not in mine_ids
    filtered = await get(client, f"{mine}/entries", account_id=theirs_account["id"])
    assert filtered.json()["data"] == []
    assert theirs_account["id"] not in {
        a["id"] for a in (await get(client, f"{mine}/accounts", limit=200)).json()["data"]
    }


async def test_anonymous_gets_401_and_non_members_get_404(
    api: Api, seeded: Seeded, other: Seeded
) -> None:
    urls = [
        f"{seeded.base}/accounts",
        f"{seeded.base}/entries",
        f"{seeded.base}/trial-balance",
    ]
    async with aclosing(api.client()) as anonymous:
        for url in urls:
            assert (await anonymous.get(url)).status_code == 401, url
    stranger, _ = await logged_in(api, other.owner)
    async with aclosing(stranger):
        for url in urls:
            assert (await stranger.get(url)).status_code == 404, url


async def test_viewer_can_read_everything(api: Api, seeded: Seeded) -> None:
    _, viewer, _ = await member(api, seeded.project_id, Role.VIEWER)
    async with aclosing(viewer):
        for suffix in ("/accounts", "/entries", "/trial-balance"):
            response = await viewer.get(f"{seeded.base}{suffix}")
            assert response.status_code == 200, suffix
        first = (await viewer.get(f"{seeded.base}/entries")).json()["data"][0]
        assert (await viewer.get(f"{seeded.base}/entries/{first['id']}")).status_code == 200


async def test_openapi_documents_every_ledger_route(api: Api) -> None:
    async with aclosing(api.client()) as anonymous:
        spec = (await anonymous.get("/openapi.json")).json()
    paths = {p: item for p, item in spec["paths"].items() if "/ledger" in p}
    assert len(paths) == 6
    for item in paths.values():
        assert set(item) == {"get"}
        assert item["get"]["summary"] and item["get"]["description"]
