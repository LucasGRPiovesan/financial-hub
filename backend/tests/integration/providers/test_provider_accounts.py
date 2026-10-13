"""Provider accounts: encrypted credentials, never returned or logged, catalog, RBAC."""

from contextlib import aclosing
from typing import Any
from uuid import UUID

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from financial_hub.modules.identity.application.api import Role
from financial_hub.modules.projects.domain import Provider, credentials_aad, encode_credentials
from financial_hub.modules.projects.infrastructure.credentials_vault import (
    CredentialsUnavailableError,
    reveal_credentials,
)
from financial_hub.platform.crypto.seal import seal
from financial_hub.shared.ids import IdPrefix, format_id, new_uuid, parse_id
from tests.integration.identity.support import (
    TEST_DATA_KEY,
    Api,
    Operator,
    csrf,
    logged_in,
    running_api,
)
from tests.integration.projects.helpers import (
    ALL_SCOPES,
    ASAAS_CREDENTIALS,
    ASAAS_SECRET,
    bearer,
    create_account,
    create_key,
    create_project,
    member,
    problem_type,
)

OTHER_DATA_KEY = "AgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgI="  # 32 bytes of 0x02


def uuid_of(account_id: str) -> UUID:
    return parse_id(account_id, IdPrefix.PROVIDER_ACCOUNT)


async def reveal(api: Api, project_id: str, account_id: str) -> dict[str, str]:
    async with api.factory() as session:
        return await reveal_credentials(
            session,
            api.app.state.keyring,
            project_id=parse_id(project_id, IdPrefix.PROJECT),
            account_id=uuid_of(account_id),
        )


async def setup(api: Api) -> tuple[Operator, dict[str, str], dict[str, Any]]:
    """A project with its owner (the creator) and one active asaas account."""
    owner = await api.operator()
    project = await create_project(api, owner)
    client, csrf_token = await logged_in(api, owner)
    async with aclosing(client):
        account = await create_account(client, csrf_token, project["id"])
    return owner, project, account


def create_body(name: str = "x", **credentials: str) -> dict[str, Any]:
    return {
        "provider": "asaas",
        "environment": "sandbox",
        "name": name,
        "credentials": credentials or ASAAS_CREDENTIALS,
    }


async def test_the_catalog_lists_providers_and_needs_authentication(api: Api) -> None:
    owner = await api.operator()
    project = await create_project(api, owner)
    client, csrf_token = await logged_in(api, owner)
    async with aclosing(client):
        catalog = await client.get("/v1/provider-catalog")
        key = await create_key(client, csrf_token, project["id"])
    async with api.client() as anonymous:
        denied = await anonymous.get("/v1/provider-catalog")
        with_key = await anonymous.get("/v1/provider-catalog", headers=bearer(key["key"]))

    entries = catalog.json()
    entries = entries["data"] if isinstance(entries, dict) else entries
    by_provider = {entry["provider"]: entry for entry in entries}
    assert catalog.status_code == 200
    assert {name: entry["enabled"] for name, entry in by_provider.items()} == {
        "asaas": True,
        "stripe": False,
        "mercadopago": False,
    }
    assert denied.status_code == 401
    assert with_key.status_code == 200


async def test_creating_an_account_never_returns_credentials(api: Api) -> None:
    owner = await api.operator()
    project = await create_project(api, owner)
    client, csrf_token = await logged_in(api, owner)
    base = f"/v1/projects/{project['id']}/provider-accounts"
    async with aclosing(client):
        response = await client.post(base, json=create_body(" Main "), headers=csrf(csrf_token))
        account = response.json()
        fetched = await client.get(f"{base}/{account['id']}")
        listed = await client.get(base)

    assert response.status_code == 201
    assert account["id"].startswith("pva_")
    assert (account["provider"], account["environment"], account["name"], account["status"]) == (
        "asaas",
        "sandbox",
        "Main",
        "active",
    )
    assert account["has_credentials"] is True
    assert account["credentials_last4"] == ASAAS_SECRET[-4:]
    assert set(account) == {
        "id",
        "project_id",
        "provider",
        "environment",
        "name",
        "status",
        "has_credentials",
        "credentials_last4",
        "created_at",
        "updated_at",
    }
    assert fetched.json() == account
    assert listed.json()["data"] == [account]
    for secret in ASAAS_CREDENTIALS.values():
        for body in (response.text, fetched.text, listed.text):
            assert secret not in body


async def test_short_secrets_get_no_last4_hint(api: Api) -> None:
    owner = await api.operator()
    project = await create_project(api, owner)
    client, csrf_token = await logged_in(api, owner)
    async with aclosing(client):
        account = await create_account(
            client, csrf_token, project["id"], credentials={"api_key": "short-key-1"}
        )

    assert account["has_credentials"] is True
    assert account["credentials_last4"] is None


async def test_the_database_holds_only_aes_gcm_ciphertext(
    api: Api, app_engine: AsyncEngine
) -> None:
    _, project, account = await setup(api)

    async with app_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT row_to_json(p)::text AS doc, key_version, nonce, ciphertext "
                    "FROM provider_accounts p WHERE id = :id"
                ),
                {"id": uuid_of(account["id"])},
            )
        ).one()

    for secret in ASAAS_CREDENTIALS.values():
        assert secret not in row.doc
        assert secret.encode() not in bytes(row.ciphertext)
    assert row.key_version == 1
    assert len(bytes(row.nonce)) == 12
    assert len(bytes(row.ciphertext)) > 16
    assert await reveal(api, project["id"], account["id"]) == ASAAS_CREDENTIALS


async def test_every_encryption_gets_its_own_nonce(api: Api, app_engine: AsyncEngine) -> None:
    owner = await api.operator()
    project = await create_project(api, owner)
    client, csrf_token = await logged_in(api, owner)
    async with aclosing(client):
        first = await create_account(client, csrf_token, project["id"], name="one")
        second = await create_account(client, csrf_token, project["id"], name="two")

    async with app_engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT nonce, ciphertext FROM provider_accounts WHERE project_id = :p"),
                {"p": parse_id(project["id"], IdPrefix.PROJECT)},
            )
        ).all()

    assert first["id"] != second["id"]
    assert len({bytes(r.nonce) for r in rows}) == 2
    assert len({bytes(r.ciphertext) for r in rows}) == 2  # same plaintext, different ciphertext


async def test_a_ciphertext_moved_to_another_account_no_longer_opens(
    api: Api, owner_engine: AsyncEngine
) -> None:
    """AAD = project|provider|account: copying the sealed columns elsewhere breaks decryption."""
    owner = await api.operator()
    project = await create_project(api, owner)
    other_project = await create_project(api, await api.operator())
    client, csrf_token = await logged_in(api, owner)
    async with aclosing(client):
        one = await create_account(client, csrf_token, project["id"], name="one")
        two = await create_account(
            client, csrf_token, project["id"], name="two", credentials={"api_key": "x" * 24}
        )

    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE provider_accounts t SET nonce = s.nonce, ciphertext = s.ciphertext, "
                "key_version = s.key_version FROM provider_accounts s "
                "WHERE s.id = :src AND t.id = :dst"
            ),
            {"src": uuid_of(one["id"]), "dst": uuid_of(two["id"])},
        )

    with pytest.raises(CredentialsUnavailableError):
        await reveal(api, project["id"], two["id"])
    # and the right account, asked through another project, is simply not found
    with pytest.raises(CredentialsUnavailableError):
        await reveal(api, other_project["id"], one["id"])
    assert await reveal(api, project["id"], one["id"]) == ASAAS_CREDENTIALS


@pytest.mark.parametrize("provider", ["stripe", "mercadopago"])
async def test_providers_that_are_not_enabled_are_422(api: Api, provider: str) -> None:
    owner = await api.operator()
    project = await create_project(api, owner)
    client, csrf_token = await logged_in(api, owner)
    secret = "sk_test_do_not_echo_0123456789"
    base = f"/v1/projects/{project['id']}/provider-accounts"
    async with aclosing(client):
        response = await client.post(
            base,
            json={
                "provider": provider,
                "environment": "sandbox",
                "name": "x",
                "credentials": {"secret_key": secret, "access_token": secret},
            },
            headers=csrf(csrf_token),
        )
        listed = await client.get(base)

    assert response.status_code == 422
    assert problem_type(response).endswith("/provider-not-enabled")
    assert secret not in response.text
    assert listed.json()["data"] == []


@pytest.mark.parametrize("provider", [Provider.STRIPE, Provider.MERCADOPAGO])
async def test_a_not_enabled_account_cannot_be_activated(
    api: Api, owner_engine: AsyncEngine, provider: Provider
) -> None:
    """Rows of a not-enabled provider can only exist if seeded; they still cannot be activated."""
    owner = await api.operator()
    project = await create_project(api, owner)
    project_uuid = parse_id(project["id"], IdPrefix.PROJECT)
    account_id = new_uuid()
    sealed = seal(
        api.app.state.keyring,
        encode_credentials({"secret_key": "irrelevant-value-1234"}),
        aad=credentials_aad(project_uuid, provider, account_id),
    )
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO provider_accounts (id, project_id, provider, environment, name, "
                "status, key_version, nonce, ciphertext, created_at, updated_at) VALUES "
                "(:id, :p, :provider, 'sandbox', 'legacy', 'disabled', :v, :n, :c, now(), now())"
            ),
            {
                "id": account_id,
                "p": project_uuid,
                "provider": provider.value,
                "v": sealed.key_version,
                "n": sealed.nonce,
                "c": sealed.ciphertext,
            },
        )
    path = (
        f"/v1/projects/{project['id']}/provider-accounts/"
        f"{format_id(IdPrefix.PROVIDER_ACCOUNT, account_id)}"
    )
    client, csrf_token = await logged_in(api, owner)
    async with aclosing(client):
        activate = await client.post(f"{path}/activate", headers=csrf(csrf_token))
        deactivate = await client.post(f"{path}/deactivate", headers=csrf(csrf_token))
        shown = await client.get(path)

    assert activate.status_code == 422
    assert problem_type(activate).endswith("/provider-not-enabled")
    assert deactivate.status_code == 200  # disabling is always allowed
    assert shown.json()["status"] == "disabled"


async def test_activate_and_deactivate_are_idempotent(api: Api) -> None:
    owner, project, account = await setup(api)
    path = f"/v1/projects/{project['id']}/provider-accounts/{account['id']}"
    client, csrf_token = await logged_in(api, owner)
    async with aclosing(client):
        off = await client.post(f"{path}/deactivate", headers=csrf(csrf_token))
        off_again = await client.post(f"{path}/deactivate", headers=csrf(csrf_token))
        on = await client.post(f"{path}/activate", headers=csrf(csrf_token))
        on_again = await client.post(f"{path}/activate", headers=csrf(csrf_token))

    assert [r.json()["status"] for r in (off, off_again, on, on_again)] == [
        "disabled",
        "disabled",
        "active",
        "active",
    ]
    assert all(r.json()["has_credentials"] is True for r in (off, on))


async def test_replacing_credentials_re_encrypts_with_a_new_nonce(
    api: Api, app_engine: AsyncEngine
) -> None:
    owner, project, account = await setup(api)
    client, csrf_token = await logged_in(api, owner)
    new_secret = "asaas_live_replacement_value_ABCD"

    async def sealed_columns() -> Any:
        async with app_engine.connect() as conn:
            return (
                await conn.execute(
                    text("SELECT nonce, ciphertext FROM provider_accounts WHERE id = :id"),
                    {"id": uuid_of(account["id"])},
                )
            ).one()

    before = await sealed_columns()
    async with aclosing(client):
        response = await client.put(
            f"/v1/projects/{project['id']}/provider-accounts/{account['id']}/credentials",
            json={"credentials": {"api_key": new_secret}},
            headers=csrf(csrf_token),
        )
    after = await sealed_columns()

    assert response.status_code == 200
    assert response.json()["credentials_last4"] == "ABCD"
    assert new_secret not in response.text
    assert bytes(after.nonce) != bytes(before.nonce)
    assert bytes(after.ciphertext) != bytes(before.ciphertext)
    assert await reveal(api, project["id"], account["id"]) == {"api_key": new_secret}


@pytest.mark.parametrize(
    "credentials",
    [
        {},
        {"api_key": "   "},
        {"api_key": "good-value-123456", "unexpected": "also-secret-value"},
        {"webhook_token": "only-optional-secret-value"},
    ],
)
async def test_invalid_credentials_are_422_without_echoing_values(
    api: Api, credentials: dict[str, str]
) -> None:
    owner = await api.operator()
    project = await create_project(api, owner)
    client, csrf_token = await logged_in(api, owner)
    base = f"/v1/projects/{project['id']}/provider-accounts"
    body = {**create_body(), "credentials": credentials}
    async with aclosing(client):
        response = await client.post(base, json=body, headers=csrf(csrf_token))
        listed = await client.get(base)

    assert response.status_code == 422
    for value in credentials.values():
        assert value.strip() == "" or value not in response.text
    assert listed.json()["data"] == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"credentials": "plain-secret-string"},
        {"credentials": ["plain-secret"]},
        {"credentials": {"api_key": 12345678901234567}},
        {"provider": "nope", "credentials": {"api_key": "plain-secret-1234"}},
        {"environment": "staging", "credentials": {"api_key": "plain-secret-1234"}},
        {"name": None, "credentials": {"api_key": "plain-secret-1234"}},
    ],
)
async def test_malformed_requests_never_echo_what_was_sent(
    api: Api, overrides: dict[str, Any]
) -> None:
    owner = await api.operator()
    project = await create_project(api, owner)
    client, csrf_token = await logged_in(api, owner)
    async with aclosing(client):
        response = await client.post(
            f"/v1/projects/{project['id']}/provider-accounts",
            json={**create_body(), **overrides},
            headers=csrf(csrf_token),
        )

    assert response.status_code == 422
    for fragment in ("plain-secret", "12345678901234567"):
        assert fragment not in response.text


async def test_account_names_are_unique_per_project_provider_and_environment(api: Api) -> None:
    owner = await api.operator()
    project = await create_project(api, owner)
    client, csrf_token = await logged_in(api, owner)
    async with aclosing(client):
        await create_account(client, csrf_token, project["id"], name="main")
        duplicate = await client.post(
            f"/v1/projects/{project['id']}/provider-accounts",
            json=create_body("main"),
            headers=csrf(csrf_token),
        )
        other_environment = await create_account(
            client, csrf_token, project["id"], name="main", environment="production"
        )

    assert duplicate.status_code == 409
    assert ASAAS_SECRET not in duplicate.text
    assert other_environment["environment"] == "production"


async def test_an_archived_project_cannot_get_new_accounts(api: Api) -> None:
    owner = await api.operator()
    project = await create_project(api, owner)
    client, csrf_token = await logged_in(api, owner)
    async with aclosing(client):
        await client.patch(
            f"/v1/projects/{project['id']}", json={"status": "archived"}, headers=csrf(csrf_token)
        )
        response = await client.post(
            f"/v1/projects/{project['id']}/provider-accounts",
            json=create_body(),
            headers=csrf(csrf_token),
        )

    assert response.status_code == 409


@pytest.mark.parametrize(
    ("role", "can_write"),
    [(Role.OWNER, True), (Role.ADMIN, True), (Role.FINANCE, False), (Role.VIEWER, False)],
)
async def test_provider_account_permissions_follow_the_role(
    api: Api, role: Role, can_write: bool
) -> None:
    """Every role reads (integrations:read); only owner and admin write (integrations:manage)."""
    _, project, account = await setup(api)
    _, client, csrf_token = await member(api, project["id"], role)
    base = f"/v1/projects/{project['id']}/provider-accounts"
    async with aclosing(client):
        listed = await client.get(base)
        fetched = await client.get(f"{base}/{account['id']}")
        created = await client.post(
            base, json=create_body(f"by-{role.value}"), headers=csrf(csrf_token)
        )
        replaced = await client.put(
            f"{base}/{account['id']}/credentials",
            json={"credentials": {"api_key": "another-valid-secret-1234"}},
            headers=csrf(csrf_token),
        )
        deactivated = await client.post(
            f"{base}/{account['id']}/deactivate", headers=csrf(csrf_token)
        )

    assert (listed.status_code, fetched.status_code) == (200, 200)
    writes = (created.status_code, replaced.status_code, deactivated.status_code)
    assert writes == ((201, 200, 200) if can_write else (403, 403, 403))


async def test_an_api_key_cannot_touch_provider_accounts(api: Api) -> None:
    owner, project, account = await setup(api)
    client, csrf_token = await logged_in(api, owner)
    async with aclosing(client):
        key = await create_key(client, csrf_token, project["id"], scopes=ALL_SCOPES)
    base = f"/v1/projects/{project['id']}/provider-accounts"
    async with api.client() as keyed:
        responses = [
            await keyed.get(base, headers=bearer(key["key"])),
            await keyed.get(f"{base}/{account['id']}", headers=bearer(key["key"])),
            await keyed.put(
                f"{base}/{account['id']}/credentials",
                json={"credentials": {"api_key": "stolen-write-attempt-1234"}},
                headers=bearer(key["key"]),
            ),
            await keyed.post(f"{base}/{account['id']}/deactivate", headers=bearer(key["key"])),
        ]

    assert [r.status_code for r in responses] == [403] * 4
    assert await reveal(api, project["id"], account["id"]) == ASAAS_CREDENTIALS


async def test_unknown_and_foreign_account_ids_are_404(api: Api) -> None:
    owner, project, _ = await setup(api)
    _, _, foreign_account = await setup(api)
    client, csrf_token = await logged_in(api, owner)
    base = f"/v1/projects/{project['id']}/provider-accounts"
    async with aclosing(client):
        responses = [
            await client.get(f"{base}/{foreign_account['id']}"),
            await client.get(f"{base}/pva_notanid"),
            await client.put(
                f"{base}/{foreign_account['id']}/credentials",
                json={"credentials": {"api_key": "foreign-takeover-attempt-1234"}},
                headers=csrf(csrf_token),
            ),
            await client.post(
                f"{base}/{foreign_account['id']}/deactivate", headers=csrf(csrf_token)
            ),
        ]

    assert [r.status_code for r in responses] == [404] * 4


async def test_credentials_never_reach_the_logs(api: Api, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("DEBUG")
    owner = await api.operator()
    project = await create_project(api, owner)
    client, csrf_token = await logged_in(api, owner)
    base = f"/v1/projects/{project['id']}/provider-accounts"
    replacement = "asaas_live_log_check_replacement_9876"
    stripe_secret = "sk_log_check_not_enabled_1234"
    async with aclosing(client):
        account = await create_account(client, csrf_token, project["id"])
        await client.put(
            f"{base}/{account['id']}/credentials",
            json={"credentials": {"api_key": replacement}},
            headers=csrf(csrf_token),
        )
        await client.post(
            base,
            json={
                "provider": "stripe",
                "environment": "sandbox",
                "name": "x",
                "credentials": {"secret_key": stripe_secret},
            },
            headers=csrf(csrf_token),
        )
        await client.post(
            base, json=create_body("bad", api_key="x" * 5000), headers=csrf(csrf_token)
        )

    logged = caplog.text + "\n".join(record.getMessage() for record in caplog.records)
    for secret in (*ASAAS_CREDENTIALS.values(), replacement, stripe_secret, TEST_DATA_KEY):
        assert secret not in logged, "a credential leaked into the logs"
    assert "provider_account.created" in logged
    assert "provider_account.credentials_replaced" in logged
    assert account["id"] in logged


async def test_old_key_versions_keep_decrypting_after_rotation() -> None:
    first = SecretStr(TEST_DATA_KEY)
    second = SecretStr(OTHER_DATA_KEY)
    async with running_api(encryption_keys={1: first}) as v1:
        owner = await v1.operator()
        project = await create_project(v1, owner)
        client, csrf_token = await logged_in(v1, owner)
        async with aclosing(client):
            old = await create_account(client, csrf_token, project["id"], name="old")

    async with running_api(encryption_keys={1: first, 2: second}) as v2:
        client, csrf_token = await logged_in(v2, owner)
        async with aclosing(client):
            new = await create_account(client, csrf_token, project["id"], name="new")
        async with v2.factory() as session:
            rows = await session.execute(
                text("SELECT name, key_version FROM provider_accounts WHERE project_id = :p"),
                {"p": parse_id(project["id"], IdPrefix.PROJECT)},
            )
            versions = {name: version for name, version in rows.all()}
        assert versions == {"old": 1, "new": 2}
        assert await reveal(v2, project["id"], old["id"]) == ASAAS_CREDENTIALS
        assert await reveal(v2, project["id"], new["id"]) == ASAAS_CREDENTIALS

    # a deployment that lost the old key cannot read the old row
    async with running_api(encryption_keys={2: second}) as lost:
        with pytest.raises(CredentialsUnavailableError):
            await reveal(lost, project["id"], old["id"])


async def test_without_encryption_keys_nothing_is_stored_and_nothing_leaks() -> None:
    async with running_api(encryption_keys={}) as bare:
        owner = await bare.operator()
        project = await create_project(bare, owner)
        client, csrf_token = await logged_in(bare, owner)
        base = f"/v1/projects/{project['id']}/provider-accounts"
        async with aclosing(client):
            response = await client.post(base, json=create_body(), headers=csrf(csrf_token))
            listed = await client.get(base)

    assert response.status_code == 500  # fails closed: the secret is never stored in the clear
    assert ASAAS_SECRET not in response.text
    assert listed.json()["data"] == []
