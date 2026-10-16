"""Document encryption, PII containment and anonymization (real Postgres)."""

import logging

import pytest
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from financial_hub.modules.customers.infrastructure.document_vault import (
    DocumentUnavailableError,
    reveal_document,
)
from financial_hub.shared.ids import IdPrefix, parse_id
from tests.integration.customers.helpers import tenant
from tests.integration.identity.support import Api
from tests.integration.projects.helpers import bearer
from tests.unit.customers.generators import format_cpf, make_cpf

NAME = "Maria Aparecida Souza"
EMAIL = "maria.souza@example.test"
PHONE = "+5511988887777"


async def rows(engine: AsyncEngine, sql: str, **params: object) -> list[tuple[object, ...]]:
    async with engine.connect() as conn:
        return [tuple(row) for row in (await conn.execute(text(sql), params)).all()]


async def reveal(api: Api, project_id: str, customer_id: str) -> str:
    async with api.factory() as session:
        return await reveal_document(
            session,
            api.app.state.keyring,
            project_id=parse_id(project_id, IdPrefix.PROJECT),
            identity_id=parse_id(customer_id, IdPrefix.CUSTOMER),
        )


async def test_document_never_appears_in_responses_logs_events_or_audit(
    api: Api, owner_engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    cpf = make_cpf()
    secrets = [cpf, format_cpf(cpf), NAME, EMAIL, PHONE]
    caplog.set_level(logging.DEBUG)
    with structlog.testing.capture_logs() as captured:
        async with tenant(api) as shop:
            created = await shop.put(
                "pii-1", name=NAME, email=EMAIL, phone=PHONE, document=format_cpf(cpf)
            )
            await shop.put(
                "pii-1", name=NAME, email=EMAIL, phone=PHONE, document=cpf, metadata={"a": 1}
            )
            failed = await shop.put("pii-2", name=NAME, document=format_cpf(cpf)[:-1] + "0")
            slug = shop.project["slug"]
            events = await rows(
                owner_engine,
                "SELECT e.data::text, coalesce(e.subject, '') FROM events e "
                "JOIN projects p ON p.id = e.project_id WHERE p.slug = :slug",
                slug=slug,
            )
            audits = await rows(
                owner_engine,
                "SELECT a.diff::text, a.target_id FROM audit_records a "
                "JOIN projects p ON p.id = a.project_id WHERE p.slug = :slug "
                "AND a.action LIKE 'customer.%'",
                slug=slug,
            )
            stored = await rows(
                owner_engine,
                "SELECT f::text FROM financial_customers f WHERE f.id IN ("
                "SELECT financial_customer_id FROM customer_identities i "
                "JOIN projects p ON p.id = i.project_id WHERE p.slug = :slug)",
                slug=slug,
            )

    assert created.status_code == 201
    assert failed.status_code == 422
    assert events and audits and stored
    logs = str(captured) + caplog.text
    for secret in secrets:
        assert secret not in logs
        assert secret not in created.text or secret in (
            NAME,
            EMAIL,
            PHONE,
        )  # identity data is returned
        assert all(secret not in str(row) for row in events + audits), secret
    assert cpf not in failed.text and format_cpf(cpf) not in failed.text
    assert all(cpf not in str(row) and format_cpf(cpf) not in str(row) for row in stored)
    assert all(cpf not in row[0] for row in stored if isinstance(row[0], str))


async def test_sealed_document_round_trips_and_is_bound_to_its_row(
    api: Api, owner_engine: AsyncEngine
) -> None:
    cpf_a, cpf_b = make_cpf(), make_cpf()
    async with tenant(api) as shop:
        first = (await shop.put("seal-a", document=cpf_a)).json()
        second = (await shop.put("seal-b", document=cpf_b)).json()
        assert await reveal(api, shop.project_id, first["id"]) == cpf_a
        assert await reveal(api, shop.project_id, second["id"]) == cpf_b

        # Moving a valid ciphertext to another universal customer must not open (AAD is the row).
        async with owner_engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE financial_customers SET document_nonce = src.n, "
                    "document_ciphertext = src.c, document_key_version = src.k FROM ("
                    "SELECT document_nonce n, document_ciphertext c, document_key_version k "
                    "FROM financial_customers WHERE id = (SELECT financial_customer_id "
                    "FROM customer_identities WHERE id = :a)) src "
                    "WHERE financial_customers.id = (SELECT financial_customer_id "
                    "FROM customer_identities WHERE id = :b)"
                ),
                {
                    "a": parse_id(first["id"], IdPrefix.CUSTOMER),
                    "b": parse_id(second["id"], IdPrefix.CUSTOMER),
                },
            )
        with pytest.raises(DocumentUnavailableError):
            await reveal(api, shop.project_id, second["id"])
        assert await reveal(api, shop.project_id, first["id"]) == cpf_a


async def test_tampered_ciphertext_is_detected(api: Api, owner_engine: AsyncEngine) -> None:
    cpf = make_cpf()
    async with tenant(api) as shop:
        created = (await shop.put("tamper", document=cpf)).json()
        identity = parse_id(created["id"], IdPrefix.CUSTOMER)
        async with owner_engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE financial_customers SET document_ciphertext = "
                    "set_byte(document_ciphertext, 0, get_byte(document_ciphertext, 0) # 255) "
                    "WHERE id = (SELECT financial_customer_id FROM customer_identities "
                    "WHERE id = :id)"
                ),
                {"id": identity},
            )
        with pytest.raises(DocumentUnavailableError):
            await reveal(api, shop.project_id, created["id"])
        # The masked display needs no decryption, so reads keep working.
        assert (await shop.get(f"/{created['id']}")).status_code == 200


async def test_reveal_is_scoped_to_the_project_and_to_documents_on_file(api: Api) -> None:
    async with tenant(api, "A") as shop_a, tenant(api, "B") as shop_b:
        with_doc = (await shop_a.put("r1", document=make_cpf())).json()
        without = (await shop_a.put("r2")).json()
        with pytest.raises(DocumentUnavailableError):
            await reveal(api, shop_b.project_id, with_doc["id"])
        with pytest.raises(DocumentUnavailableError):
            await reveal(api, shop_a.project_id, without["id"])


async def test_anonymize_one_of_two_projects_keeps_the_shared_record(
    api: Api, owner_engine: AsyncEngine
) -> None:
    cpf = make_cpf()
    async with tenant(api, "A") as shop_a, tenant(api, "B") as shop_b:
        mine = (await shop_a.put("a", name=NAME, email=EMAIL, phone=PHONE, document=cpf)).json()
        theirs = (await shop_b.put("b", name="Maria", email="m@example.test", document=cpf)).json()

        done = await shop_a.client.post(
            f"{shop_a.base}/{mine['id']}/anonymize", headers={"X-CSRF-Token": shop_a.csrf_token}
        )
        shared = await rows(
            owner_engine,
            "SELECT f.name, f.anonymized_at IS NOT NULL, f.document_hash IS NOT NULL "
            "FROM financial_customers f WHERE f.id = (SELECT financial_customer_id "
            "FROM customer_identities WHERE id = :id)",
            id=parse_id(theirs["id"], IdPrefix.CUSTOMER),
        )
        b_still = (await shop_b.get(f"/{theirs['id']}")).json()
        again = await shop_a.client.post(
            f"{shop_a.base}/{mine['id']}/anonymize", headers={"X-CSRF-Token": shop_a.csrf_token}
        )
        reput = await shop_a.put("a", name=NAME, kind="individual")

        last = await shop_b.client.post(
            f"{shop_b.base}/{theirs['id']}/anonymize", headers={"X-CSRF-Token": shop_b.csrf_token}
        )
        wiped = await rows(
            owner_engine,
            "SELECT f.name, f.email, f.phone, f.document_hash, f.document_ciphertext, "
            "f.document_last4, f.anonymized_at IS NOT NULL FROM financial_customers f "
            "WHERE f.id = (SELECT financial_customer_id FROM customer_identities WHERE id = :id)",
            id=parse_id(theirs["id"], IdPrefix.CUSTOMER),
        )
        audit = await rows(
            owner_engine,
            "SELECT a.action FROM audit_records a JOIN projects p ON p.id = a.project_id "
            "WHERE p.slug = :slug AND a.action = 'customer.anonymized'",
            slug=shop_a.project["slug"],
        )
        after_wipe = await shop_a.put("a-new", name="New", document=cpf)

    assert done.status_code == 200
    assert done.json()["name"] == "anonymized"
    assert done.json()["email"] is None and done.json()["phone"] is None
    assert done.json()["anonymized"] is True and done.json()["metadata"] == {}
    assert done.json()["document_masked"] is not None  # last digits belong to the shared record
    assert shared == [(NAME, False, True)]  # first-seen record of the person, still live
    assert b_still["name"] == "Maria" and b_still["document_masked"].endswith(cpf[-2:])
    assert again.status_code == 200 and again.json() == done.json()  # idempotent
    assert reput.status_code == 409
    assert last.status_code == 200
    assert wiped == [("anonymized", None, None, None, None, None, True)]
    assert len(audit) == 1  # the repeat did not audit twice
    assert last.json()["document_masked"] is None
    # The wiped universal record no longer matches the document: it is a brand new person.
    assert after_wipe.status_code == 201


async def test_anonymize_sole_identity_wipes_the_universal_record(
    api: Api, owner_engine: AsyncEngine
) -> None:
    async with tenant(api) as shop:
        customer = (await shop.put("solo", name=NAME, email=EMAIL, document=make_cpf())).json()
        response = await shop.client.post(
            f"{shop.base}/{customer['id']}/anonymize",
            headers={"X-CSRF-Token": shop.csrf_token},
        )
        universal = await rows(
            owner_engine,
            "SELECT f.name, f.email, f.document_hash, f.document_ciphertext "
            "FROM financial_customers f WHERE f.id = (SELECT financial_customer_id "
            "FROM customer_identities WHERE id = :id)",
            id=parse_id(customer["id"], IdPrefix.CUSTOMER),
        )
        listed = await shop.get()
        events = await rows(
            owner_engine,
            "SELECT e.data::text FROM events e JOIN projects p ON p.id = e.project_id "
            "WHERE p.slug = :slug AND e.type = 'customer.anonymized'",
            slug=shop.project["slug"],
        )

    assert response.status_code == 200
    assert universal == [("anonymized", None, None, None)]
    assert listed.json()["data"][0]["name"] == "anonymized"  # the row stays: ledger refs it
    assert len(events) == 1 and NAME not in str(events)


async def test_anonymize_unknown_customer_is_404_and_needs_a_session(api: Api) -> None:
    async with tenant(api) as shop:
        missing = await shop.client.post(
            f"{shop.base}/cus_00000000000000000000000000/anonymize",
            headers={"X-CSRF-Token": shop.csrf_token},
        )
        malformed = await shop.client.post(
            f"{shop.base}/nope/anonymize", headers={"X-CSRF-Token": shop.csrf_token}
        )
        async with api.client() as anonymous:
            unauthenticated = await anonymous.post(
                f"{shop.base}/cus_00000000000000000000000000/anonymize"
            )
            by_key = await anonymous.post(
                f"{shop.base}/cus_00000000000000000000000000/anonymize",
                headers=bearer(shop.write_key),
            )

    assert missing.status_code == malformed.status_code == 404
    assert unauthenticated.status_code == 401
    assert by_key.status_code == 403
