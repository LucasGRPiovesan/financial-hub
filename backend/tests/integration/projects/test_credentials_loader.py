"""The one door to decrypted provider credentials (PRO-41): active accounts of the project."""

from contextlib import aclosing
from uuid import UUID

from financial_hub.modules.projects.application.api import ProviderSecrets, load_provider_secrets
from financial_hub.shared.ids import IdPrefix, parse_id
from tests.integration.identity.support import Api, csrf, logged_in
from tests.integration.projects.helpers import create_account, create_project

FAKE_KEY = "fake-loader-key-5b2f81"
FAKE_TOKEN = "fake-loader-webhook-9a07"


async def load(api: Api, project_id: str, account_id: str) -> ProviderSecrets | None:
    return await load_by_uuid(
        api,
        parse_id(project_id, IdPrefix.PROJECT),
        parse_id(account_id, IdPrefix.PROVIDER_ACCOUNT),
    )


async def load_by_uuid(api: Api, project_id: UUID, account_id: UUID) -> ProviderSecrets | None:
    async with api.factory() as session:
        return await load_provider_secrets(
            session, api.app.state.keyring, project_id=project_id, account_id=account_id
        )


async def test_an_active_account_yields_its_secrets_and_identity(api: Api) -> None:
    owner = await api.operator()
    project = await create_project(api, owner)
    client, token = await logged_in(api, owner)
    async with aclosing(client):
        account = await create_account(
            client,
            token,
            project["id"],
            credentials={"api_key": FAKE_KEY, "webhook_token": FAKE_TOKEN},
        )

    secrets = await load(api, project["id"], account["id"])

    assert secrets is not None
    assert (secrets.provider, secrets.environment) == ("asaas", "sandbox")
    assert dict(secrets.values) == {"api_key": FAKE_KEY, "webhook_token": FAKE_TOKEN}


async def test_the_repr_and_str_never_show_the_secrets(api: Api) -> None:
    owner = await api.operator()
    project = await create_project(api, owner)
    client, token = await logged_in(api, owner)
    async with aclosing(client):
        account = await create_account(
            client, token, project["id"], credentials={"api_key": FAKE_KEY}
        )

    secrets = await load(api, project["id"], account["id"])

    assert secrets is not None
    for text in (repr(secrets), str(secrets), f"{secrets!r}", f"{secrets}"):
        assert FAKE_KEY not in text
    assert "asaas" in repr(secrets)


async def test_a_deactivated_account_is_indistinguishable_from_a_missing_one(api: Api) -> None:
    owner = await api.operator()
    project = await create_project(api, owner)
    client, token = await logged_in(api, owner)
    async with aclosing(client):
        account = await create_account(
            client, token, project["id"], credentials={"api_key": FAKE_KEY}
        )
        before = await load(api, project["id"], account["id"])
        off = await client.post(
            f"/v1/projects/{project['id']}/provider-accounts/{account['id']}/deactivate",
            headers=csrf(token),
        )
        assert off.status_code == 200, off.text
        disabled = await load(api, project["id"], account["id"])
        on = await client.post(
            f"/v1/projects/{project['id']}/provider-accounts/{account['id']}/activate",
            headers=csrf(token),
        )
        assert on.status_code == 200, on.text
        again = await load(api, project["id"], account["id"])

    assert before is not None and again is not None
    assert disabled is None


async def test_another_projects_account_is_never_returned(api: Api) -> None:
    owner = await api.operator()
    mine = await create_project(api, owner, "Mine")
    theirs = await create_project(api, owner, "Theirs")
    client, token = await logged_in(api, owner)
    async with aclosing(client):
        foreign = await create_account(
            client, token, theirs["id"], credentials={"api_key": FAKE_KEY}
        )

    assert await load(api, mine["id"], foreign["id"]) is None
    assert await load(api, theirs["id"], foreign["id"]) is not None


async def test_an_unknown_account_yields_nothing(api: Api) -> None:
    owner = await api.operator()
    project = await create_project(api, owner)

    missing = await load(api, project["id"], "pva_00000000000000000000000000")

    assert missing is None
