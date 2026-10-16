"""Upsert, reads, validation, permissions and isolation of the customers API (real Postgres)."""

import asyncio
from contextlib import aclosing

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from financial_hub.modules.identity.application.api import Role
from tests.integration.customers.helpers import Tenant, tenant
from tests.integration.identity.support import Api, csrf
from tests.integration.projects.helpers import bearer, create_key, member
from tests.unit.customers.generators import (
    format_cnpj,
    format_cpf,
    make_cnpj,
    make_cpf,
    with_bad_check_digit,
)

BODY = {"name": "Maria Silva", "kind": "individual", "email": "Maria@Example.test"}


async def count(engine: AsyncEngine, sql: str, **params: object) -> int:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar_one()


async def test_upsert_is_idempotent_and_reports_201_then_200(api: Api) -> None:
    async with tenant(api) as shop:
        created = await shop.put("user-1", **BODY)
        again = await shop.put("user-1", **BODY)
        changed = await shop.put(
            "user-1", **{**BODY, "name": "Maria S. Silva", "phone": "+5511999990000"}
        )
        by_ref = await shop.get("/by-ref/user-1")

    assert created.status_code == 201, created.text
    assert again.status_code == 200
    assert again.json() == created.json()
    assert created.json()["id"].startswith("cus_")
    assert created.json()["email"] == "maria@example.test"
    assert created.json()["document_masked"] is None
    assert changed.status_code == 200
    assert changed.json()["id"] == created.json()["id"]
    assert changed.json()["name"] == "Maria S. Silva"
    assert changed.json()["phone"] == "+5511999990000"
    assert by_ref.json() == changed.json()


async def test_reads_by_id_ref_list_filters_and_pagination(api: Api) -> None:
    async with tenant(api) as shop:
        ids = []
        for number in range(3):
            response = await shop.put(f"u{number}", **{**BODY, "email": f"u{number}@example.test"})
            ids.append(response.json()["id"])
        by_id = await shop.get(f"/{ids[1]}")
        first = await shop.get("", limit=2)
        second = await shop.get("", limit=2, cursor=first.json()["next_cursor"])
        by_email = await shop.get("", email="U2@example.test")
        by_ref = await shop.get("", external_ref="u0")
        missing = await shop.get("/by-ref/nope")
        malformed = await shop.get("/not-an-id")
        wrong_prefix = await shop.get(f"/{ids[0].replace('cus_', 'inv_')}")
        bad_email = await shop.get("", email="nope")

    assert by_id.json()["external_ref"] == "u1"
    assert [row["id"] for row in first.json()["data"]] == [ids[2], ids[1]]
    assert first.json()["has_more"] is True
    assert [row["id"] for row in second.json()["data"]] == [ids[0]]
    assert second.json()["has_more"] is False
    assert [row["id"] for row in by_email.json()["data"]] == [ids[2]]
    assert [row["id"] for row in by_ref.json()["data"]] == [ids[0]]
    assert (missing.status_code, malformed.status_code, wrong_prefix.status_code) == (404, 404, 404)
    assert bad_email.status_code == 422


async def test_same_document_in_two_projects_is_one_universal_customer(
    api: Api, owner_engine: AsyncEngine
) -> None:
    cpf = make_cpf()
    async with tenant(api, "A") as shop_a, tenant(api, "B") as shop_b:
        in_a = await shop_a.put("a-1", document=format_cpf(cpf), **BODY)
        in_b = await shop_b.put("b-1", document=cpf, name="M. Silva", kind="individual")
        a_reads = await shop_a.get("")
        b_reads = await shop_b.get("")
        cross = await shop_b.get(f"/{in_a.json()['id']}")
        listed_in_b = await shop_b.get("", external_ref="a-1")
        universal = await count(
            owner_engine,
            "SELECT count(DISTINCT financial_customer_id) FROM customer_identities "
            "WHERE project_id = ANY(SELECT id FROM projects WHERE slug = ANY(:slugs))",
            slugs=[shop_a.project["slug"], shop_b.project["slug"]],
        )
        identities = await count(
            owner_engine,
            "SELECT count(*) FROM customer_identities i JOIN projects p ON p.id = i.project_id "
            "WHERE p.slug = ANY(:slugs)",
            slugs=[shop_a.project["slug"], shop_b.project["slug"]],
        )

    assert in_a.status_code == in_b.status_code == 201
    assert in_a.json()["id"] != in_b.json()["id"]
    assert (
        in_a.json()["document_masked"]
        == in_b.json()["document_masked"]
        == f"***.***.***-{cpf[-2:]}"
    )
    assert (universal, identities) == (1, 2)
    assert [row["external_ref"] for row in a_reads.json()["data"]] == ["a-1"]
    assert [row["external_ref"] for row in b_reads.json()["data"]] == ["b-1"]
    assert b_reads.json()["data"][0]["name"] == "M. Silva"  # identity data is project-private
    assert a_reads.json()["data"][0]["name"] == "Maria Silva"
    assert cross.status_code == 404
    assert listed_in_b.json()["data"] == []
    for response in (in_a, in_b, a_reads, b_reads):
        assert "fcus_" not in response.text
        assert cpf not in response.text


async def test_concurrent_upserts_of_one_ref_create_one_identity(
    api: Api, owner_engine: AsyncEngine
) -> None:
    async with tenant(api) as shop:
        shared_document = make_cpf()
        responses = await asyncio.gather(
            *(shop.put("race", document=shared_document, **BODY) for _ in range(8))
        )
        rows = await count(
            owner_engine,
            "SELECT count(*) FROM customer_identities i JOIN projects p ON p.id = i.project_id "
            "WHERE p.slug = :slug",
            slug=shop.project["slug"],
        )

    assert sorted(r.status_code for r in responses) == [200] * 7 + [201]
    assert len({r.json()["id"] for r in responses}) == 1
    assert rows == 1


async def test_concurrent_upserts_of_one_document_share_one_universal_customer(
    api: Api, owner_engine: AsyncEngine
) -> None:
    cpf = make_cpf()
    async with tenant(api) as shop_a, tenant(api) as shop_b:
        responses = await asyncio.gather(
            *(shop_a.put(f"a-{i}", document=cpf, **BODY) for i in range(5)),
            *(shop_b.put(f"b-{i}", document=format_cpf(cpf), **BODY) for i in range(5)),
        )
        slugs = [shop_a.project["slug"], shop_b.project["slug"]]
        universal = await count(
            owner_engine,
            "SELECT count(DISTINCT i.financial_customer_id) FROM customer_identities i "
            "JOIN projects p ON p.id = i.project_id WHERE p.slug = ANY(:slugs)",
            slugs=slugs,
        )
        identities = await count(
            owner_engine,
            "SELECT count(*) FROM customer_identities i JOIN projects p ON p.id = i.project_id "
            "WHERE p.slug = ANY(:slugs)",
            slugs=slugs,
        )
        holders = await count(
            owner_engine,
            "SELECT count(*) FROM financial_customers f WHERE f.id IN ("
            "SELECT i.financial_customer_id FROM customer_identities i "
            "JOIN projects p ON p.id = i.project_id WHERE p.slug = ANY(:slugs))",
            slugs=slugs,
        )

    assert all(r.status_code == 201 for r in responses), [r.text for r in responses]
    assert (universal, identities, holders) == (1, 10, 1)


async def test_cpf_and_cnpj_validation(api: Api) -> None:
    cpf, cnpj = make_cpf(), make_cnpj()
    async with tenant(api) as shop:
        valid_cpf = await shop.put("p1", document=format_cpf(cpf), **BODY)
        valid_cnpj = await shop.put(
            "c1", document=format_cnpj(cnpj), name="ACME Ltda", kind="company"
        )
        bad_cpf = await shop.put("p2", document=with_bad_check_digit(cpf), **BODY)
        bad_cnpj = await shop.put(
            "c2", document=with_bad_check_digit(cnpj), name="ACME", kind="company"
        )
        repeated = await shop.put("p3", document="11111111111", **BODY)
        letters = await shop.put("p4", document="ABC", **BODY)
        wrong_kind = await shop.put("p5", document=cnpj, **BODY)
        wrong_kind_cpf = await shop.put("p6", document=cpf, name="X", kind="company")
        bad_kind = await shop.put("p7", **{**BODY, "kind": "alien"})
        bad_ref = await shop.put("has space", **BODY)
        bad_email = await shop.put("p8", **{**BODY, "email": "nope"})
        bad_phone = await shop.put("p9", **{**BODY, "phone": "12"})
        bad_meta = await shop.put("p10", **{**BODY, "metadata": {"k" * 50: 1}})
        empty_name = await shop.put("p11", **{**BODY, "name": "  "})

    assert valid_cpf.status_code == 201 and valid_cpf.json()["document_masked"].endswith(cpf[-2:])
    assert valid_cnpj.status_code == 201
    assert valid_cnpj.json()["document_masked"] == f"**.***.***/****-{cnpj[-2:]}"
    for response in (
        bad_cpf, bad_cnpj, repeated, letters, wrong_kind, wrong_kind_cpf,
        bad_kind, bad_ref, bad_email, bad_phone, bad_meta, empty_name,
    ):  # fmt: skip
        assert response.status_code == 422, response.text
    for response in (bad_cpf, bad_cnpj, wrong_kind):
        assert cpf not in response.text and cnpj not in response.text


async def test_document_can_be_added_relinked_but_not_changed(
    api: Api, owner_engine: AsyncEngine
) -> None:
    first, second = make_cpf(), make_cpf()
    async with tenant(api) as shop_a, tenant(api) as shop_b:
        await shop_b.put("b-1", document=first, **BODY)
        plain = await shop_a.put("a-1", **BODY)
        attached = await shop_a.put("a-1", document=second, **BODY)
        same = await shop_a.put("a-1", document=format_cpf(second), **BODY)
        change = await shop_a.put("a-1", document=first, **BODY)
        kind_change = await shop_a.put("a-1", **{**BODY, "kind": "company"})
        # An identity without a document later joins the person already known by it.
        await shop_a.put("a-2", **BODY)
        relinked = await shop_a.put("a-2", document=first, **BODY)
        wiped = await count(
            owner_engine,
            "SELECT count(*) FROM financial_customers WHERE anonymized_at IS NOT NULL "
            "AND name = 'anonymized'",
        )
        shared = await count(
            owner_engine,
            "SELECT count(DISTINCT i.financial_customer_id) FROM customer_identities i "
            "JOIN projects p ON p.id = i.project_id "
            "WHERE p.slug = ANY(:slugs) AND i.external_ref IN ('b-1', 'a-2')",
            slugs=[shop_a.project["slug"], shop_b.project["slug"]],
        )

    assert plain.status_code == 201 and plain.json()["document_masked"] is None
    assert attached.status_code == 200
    assert attached.json()["document_masked"] == f"***.***.***-{second[-2:]}"
    assert same.status_code == 200 and same.json()["updated_at"] == attached.json()["updated_at"]
    assert change.status_code == 409
    assert kind_change.status_code == 422
    assert relinked.status_code == 200
    assert shared == 1
    assert wiped >= 1  # the document-less record a-2 left behind carried nothing: it was wiped


async def test_unchanged_repeat_writes_no_audit_or_event_row(
    api: Api, owner_engine: AsyncEngine
) -> None:
    async with tenant(api) as shop:
        await shop.put("quiet", **BODY)
        before = await counts(owner_engine, shop)
        await shop.put("quiet", **BODY)
        assert await counts(owner_engine, shop) == before
        await shop.put("quiet", **{**BODY, "name": "Other"})
        after = await counts(owner_engine, shop)

    assert before == (1, 1)
    assert after == (2, 2)


async def counts(engine: AsyncEngine, shop: Tenant) -> tuple[int, int]:
    pid = shop.project_id
    del pid
    audit = await count(
        engine,
        "SELECT count(*) FROM audit_records a JOIN projects p ON p.id = a.project_id "
        "WHERE p.slug = :slug AND a.action LIKE 'customer.%'",
        slug=shop.project["slug"],
    )
    events = await count(
        engine,
        "SELECT count(*) FROM events e JOIN projects p ON p.id = e.project_id "
        "WHERE p.slug = :slug AND e.type LIKE 'customer.%'",
        slug=shop.project["slug"],
    )
    return audit, events


async def test_permissions_by_key_scope_and_role(api: Api) -> None:
    async with tenant(api) as shop:
        client, csrf_token = shop.client, shop.csrf_token
        read_key = (
            await create_key(client, csrf_token, shop.project_id, scopes=["customers:read"])
        )["key"]
        other_key = (
            await create_key(client, csrf_token, shop.project_id, scopes=["projects:read"])
        )["key"]
        seeded = await shop.put("seed", **BODY)
        forbidden_put = await shop.put("x", via_key=False, **BODY)  # operator owner: allowed
        async with api.client() as anonymous:
            put_with_read_key = await anonymous.put(
                f"{shop.base}/x2", json=BODY, headers=bearer(read_key)
            )
            put_without_credentials = await anonymous.put(f"{shop.base}/x3", json=BODY)
        read_ok = await shop.get("", key=read_key)
        read_denied = await shop.get("", key=other_key)
        anonymize_by_key = await shop.client.post(
            f"{shop.base}/{seeded.json()['id']}/anonymize", headers=bearer(shop.write_key)
        )
        _, viewer, viewer_csrf = await member(api, shop.project_id, Role.VIEWER)
        async with aclosing(viewer):
            viewer_put = await viewer.put(f"{shop.base}/v", json=BODY, headers=csrf(viewer_csrf))
            viewer_read = await viewer.get(shop.base)
            viewer_anonymize = await viewer.post(
                f"{shop.base}/{seeded.json()['id']}/anonymize", headers=csrf(viewer_csrf)
            )

    assert forbidden_put.status_code == 201
    assert put_with_read_key.status_code == 403
    assert put_without_credentials.status_code == 401
    assert read_ok.status_code == 200
    assert read_denied.status_code == 403
    assert anonymize_by_key.status_code in (401, 403)
    assert viewer_put.status_code == 403
    assert viewer_read.status_code == 200
    assert viewer_anonymize.status_code == 403


async def test_api_key_anonymize_is_forbidden_even_with_write_scope(api: Api) -> None:
    async with tenant(api) as shop:
        seeded = await shop.put("seed", **BODY)
        async with api.client() as client:
            response = await client.post(
                f"{shop.base}/{seeded.json()['id']}/anonymize", headers=bearer(shop.write_key)
            )
        still = await shop.get(f"/{seeded.json()['id']}")

    assert response.status_code == 403
    assert still.json()["name"] == "Maria Silva"


async def test_one_project_never_reads_or_writes_anothers_customers(api: Api) -> None:
    async with tenant(api, "A") as shop_a, tenant(api, "B") as shop_b:
        mine = await shop_a.put("same-ref", **BODY)
        theirs = await shop_b.put("same-ref", name="Someone Else", kind="individual")
        a_key_on_b = await shop_a.get("", key=shop_a.write_key)
        async with api.client() as client:
            cross_get = await client.get(
                f"/v1/projects/{shop_b.project_id}/customers", headers=bearer(shop_a.write_key)
            )
            cross_put = await client.put(
                f"/v1/projects/{shop_b.project_id}/customers/x",
                json=BODY,
                headers=bearer(shop_a.write_key),
            )

    assert mine.json()["id"] != theirs.json()["id"]  # same ref, different projects: two identities
    assert [row["name"] for row in a_key_on_b.json()["data"]] == ["Maria Silva"]
    assert cross_get.status_code == cross_put.status_code == 404


async def test_idempotency_key_replays_the_first_response(api: Api) -> None:
    async with tenant(api) as shop:
        headers = {"Idempotency-Key": "fake-key-0001"}
        first = await shop.put("idem", headers=headers, **BODY)
        replay = await shop.put("idem", headers=headers, **BODY)
        conflict = await shop.put("idem", headers=headers, **{**BODY, "name": "Different"})

    assert first.status_code == 201
    assert replay.status_code == 201
    assert replay.json() == first.json()
    assert conflict.status_code in (409, 422)


async def test_response_never_contains_the_document_or_universal_ids(api: Api) -> None:
    cpf = make_cpf()
    async with tenant(api) as shop:
        responses: list[httpx.Response] = [await shop.put("leak", document=cpf, **BODY)]
        customer_id = responses[0].json()["id"]
        responses += [
            await shop.get(),
            await shop.get(f"/{customer_id}"),
            await shop.get("/by-ref/leak"),
        ]

    for response in responses:
        assert cpf not in response.text
        assert format_cpf(cpf) not in response.text
        assert "fcus" not in response.text
        assert "document_hash" not in response.text
