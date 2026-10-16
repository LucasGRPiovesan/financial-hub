"""Every M1 privileged action writes exactly one audit record, in the action's own transaction."""

from contextlib import aclosing
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from financial_hub.modules.audit.application import (
    Actor,
    AuditContext,
    AuditRecorder,
    AuditTarget,
)
from financial_hub.platform.db import UnitOfWork
from financial_hub.shared.ids import IdPrefix, parse_id
from tests.integration.audit.helpers import (
    dump_table,
    new_correlation_id,
    only_row,
    rows_for,
    tagged,
)
from tests.integration.identity.support import Api, csrf, logged_in, login, unique_slug
from tests.integration.projects.helpers import (
    ASAAS_CREDENTIALS,
    ASAAS_SECRET,
    create_account,
    create_key,
    create_project,
)


def project_uuid(project_id: str) -> UUID:
    return parse_id(project_id, IdPrefix.PROJECT)


async def test_login_success_and_logout(api: Api, app_engine: AsyncEngine) -> None:
    operator = await api.operator()
    async with api.client() as client:
        cid = tagged(client)
        client.headers["User-Agent"] = "audit-tests/1.0"
        response = await login(client, operator)
        assert response.status_code == 200
        record = await only_row(app_engine, cid)
        assert (record["actor_type"], record["actor_id"]) == ("operator", operator.id)
        assert (record["action"], record["target_type"], record["target_id"]) == (
            "operator.login_succeeded",
            "operator",
            operator.id,
        )
        assert record["project_id"] is None
        assert record["user_agent"] == "audit-tests/1.0"
        assert record["request_id"]
        assert record["ip"] == api.ip

        cid = tagged(client)
        out = await client.post("/v1/auth/logout", headers=csrf(response.json()["csrf_token"]))
        assert out.status_code == 204
    logout = await only_row(app_engine, cid)
    assert (logout["actor_type"], logout["actor_id"], logout["action"]) == (
        "operator",
        operator.id,
        "operator.logout",
    )
    assert (logout["target_type"], logout["target_id"]) == ("operator", operator.id)


async def test_failed_login_is_recorded_once_with_a_masked_email(
    api: Api, app_engine: AsyncEngine
) -> None:
    operator = await api.operator()
    async with api.client() as client:
        cid = tagged(client)
        response = await login(client, operator, password="wrong password!")
    assert response.status_code == 401
    record = await only_row(app_engine, cid)
    assert (record["actor_type"], record["action"]) == ("system", "operator.login_failed")
    assert (record["target_type"], record["target_id"]) == ("operator", operator.id)
    assert record["diff"]["reason"]
    assert operator.email not in str(record["diff"])
    assert record["diff"]["email"] != operator.email


async def test_unknown_email_failure_has_no_target_id(api: Api, app_engine: AsyncEngine) -> None:
    ghost = await api.operator()
    async with api.client() as client:
        cid = tagged(client)
        response = await client.post(
            "/v1/auth/login", json={"email": "nobody-" + ghost.email, "password": "whatever-123"}
        )
    assert response.status_code == 401
    record = await only_row(app_engine, cid)
    assert (record["action"], record["target_type"], record["target_id"]) == (
        "operator.login_failed",
        "operator",
        None,
    )


async def test_the_failure_that_locks_the_account_records_failed_and_locked(
    api: Api, app_engine: AsyncEngine
) -> None:
    operator = await api.operator()
    async with api.client() as client:
        for _ in range(4):
            assert (await login(client, operator, password="wrong password!")).status_code == 401
        cid = tagged(client)
        assert (await login(client, operator, password="wrong password!")).status_code == 401
        locked_rows = await rows_for(app_engine, cid)
        assert sorted(row["action"] for row in locked_rows) == [
            "operator.login_failed",
            "operator.login_locked",
        ]
        assert {row["target_id"] for row in locked_rows} == {operator.id}

        cid = tagged(client)
        blocked = await login(client, operator)
        assert blocked.status_code == 429
        assert await rows_for(app_engine, cid) == []  # throttled attempts are only logged

        api.clock.advance(timedelta(minutes=16))
        assert (await login(client, operator)).status_code == 200


async def test_password_change_is_recorded(api: Api, app_engine: AsyncEngine) -> None:
    operator = await api.operator()
    client, token = await logged_in(api, operator)
    async with aclosing(client):
        cid = tagged(client)
        response = await client.post(
            "/v1/auth/password",
            json={"current_password": operator.password, "new_password": "a brand new passphrase"},
            headers=csrf(token),
        )
    assert response.status_code == 204, response.text
    record = await only_row(app_engine, cid)
    assert (record["actor_id"], record["action"], record["target_id"]) == (
        operator.id,
        "operator.password_changed",
        operator.id,
    )
    assert "passphrase" not in str(record["diff"])


async def test_project_create_and_update(api: Api, app_engine: AsyncEngine) -> None:
    owner = await api.operator()
    client, token = await logged_in(api, owner)
    async with aclosing(client):
        cid = tagged(client)
        slug = unique_slug()
        created = await client.post(
            "/v1/projects", json={"slug": slug, "name": "Original"}, headers=csrf(token)
        )
        assert created.status_code == 201
        project = created.json()
        by_action = {row["action"]: row for row in await rows_for(app_engine, cid)}
        assert set(by_action) == {"project.created", "member.added"}
        made = by_action["project.created"]
        assert (made["actor_type"], made["actor_id"]) == ("operator", owner.id)
        assert (made["target_type"], made["target_id"]) == ("project", project["id"])
        assert made["project_id"] == project_uuid(project["id"])
        assert made["diff"] == {"after": {"slug": slug, "name": "Original"}}
        added = by_action["member.added"]
        assert (added["target_type"], added["target_id"]) == ("operator", owner.id)
        assert added["diff"] == {"after": {"role": "owner"}}
        assert added["project_id"] == project_uuid(project["id"])

        cid = tagged(client)
        patched = await client.patch(
            f"/v1/projects/{project['id']}", json={"name": "Renamed"}, headers=csrf(token)
        )
        assert patched.status_code == 200
        updated = await only_row(app_engine, cid)
        assert updated["action"] == "project.updated"
        assert (updated["target_type"], updated["target_id"]) == ("project", project["id"])
        assert updated["diff"] == {"before": {"name": "Original"}, "after": {"name": "Renamed"}}

        cid = tagged(client)
        again = await client.patch(
            f"/v1/projects/{project['id']}", json={"name": "Renamed"}, headers=csrf(token)
        )
        assert again.status_code == 200
        assert await rows_for(app_engine, cid) == []  # nothing changed, nothing recorded


async def test_api_key_create_and_revoke_leave_no_secret(api: Api, app_engine: AsyncEngine) -> None:
    owner = await api.operator()
    project = await create_project(api, owner)
    client, token = await logged_in(api, owner)
    async with aclosing(client):
        cid = tagged(client)
        created = await create_key(client, token, project["id"], scopes=["ledger:read"])
        record = await only_row(app_engine, cid)
        assert (record["actor_type"], record["actor_id"]) == ("operator", owner.id)
        assert (record["action"], record["target_type"], record["target_id"]) == (
            "api_key.created",
            "api_key",
            created["id"],
        )
        assert record["project_id"] == project_uuid(project["id"])
        assert record["diff"]["after"]["prefix"] == created["prefix"]
        assert record["diff"]["after"]["scopes"] == ["ledger:read"]

        cid = tagged(client)
        revoked = await client.post(
            f"/v1/projects/{project['id']}/api-keys/{created['id']}/revoke", headers=csrf(token)
        )
        assert revoked.status_code == 200
        revoke_row = await only_row(app_engine, cid)
        assert (revoke_row["action"], revoke_row["target_id"]) == ("api_key.revoked", created["id"])

        cid = tagged(client)
        repeat = await client.post(
            f"/v1/projects/{project['id']}/api-keys/{created['id']}/revoke", headers=csrf(token)
        )
        assert repeat.status_code == 200
        assert await rows_for(app_engine, cid) == []  # idempotent revoke records nothing

    async with app_engine.connect() as conn:
        digest = (
            await conn.execute(
                text("SELECT encode(secret_hash, 'hex') FROM api_keys WHERE prefix = :p"),
                {"p": created["prefix"]},
            )
        ).scalar_one_or_none()
    dumped = await dump_table(app_engine)
    assert created["key"] not in dumped
    assert created["key"].rsplit("_", 1)[1] not in dumped
    if digest:
        assert digest not in dumped


async def test_provider_account_lifecycle_and_no_credentials(
    api: Api, app_engine: AsyncEngine
) -> None:
    owner = await api.operator()
    project = await create_project(api, owner)
    client, token = await logged_in(api, owner)
    async with aclosing(client):
        cid = tagged(client)
        account = await create_account(client, token, project["id"])
        created = await only_row(app_engine, cid)
        assert (created["action"], created["target_type"], created["target_id"]) == (
            "provider_account.created",
            "provider_account",
            account["id"],
        )
        assert created["diff"]["after"]["provider"] == "asaas"
        assert created["diff"]["after"]["credentials"] == "[redacted]"
        assert created["project_id"] == project_uuid(project["id"])

        base = f"/v1/projects/{project['id']}/provider-accounts/{account['id']}"
        new_secret = "fake-asaas-key-ZZZZ9999"
        cid = tagged(client)
        replaced = await client.put(
            f"{base}/credentials",
            json={"credentials": {"api_key": new_secret}},
            headers=csrf(token),
        )
        assert replaced.status_code == 200
        replaced_row = await only_row(app_engine, cid)
        assert replaced_row["action"] == "provider_account.credentials_replaced"
        assert "credentials" in replaced_row["diff"]["after"]

        cid = tagged(client)
        assert (await client.post(f"{base}/deactivate", headers=csrf(token))).status_code == 200
        assert (await only_row(app_engine, cid))["action"] == "provider_account.disabled"

        cid = tagged(client)
        assert (await client.post(f"{base}/deactivate", headers=csrf(token))).status_code == 200
        assert await rows_for(app_engine, cid) == []  # no change, no record

        cid = tagged(client)
        assert (await client.post(f"{base}/activate", headers=csrf(token))).status_code == 200
        assert (await only_row(app_engine, cid))["action"] == "provider_account.activated"

    dumped = await dump_table(app_engine)
    for secret in (ASAAS_SECRET, new_secret, *ASAAS_CREDENTIALS.values()):
        assert secret not in dumped


async def test_a_rolled_back_action_leaves_no_audit_row(api: Api, app_engine: AsyncEngine) -> None:
    probe = new_correlation_id()
    context = AuditContext(Actor.system(), correlation_id=probe)

    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        async with UnitOfWork(api.factory) as uow:
            await AuditRecorder.record(
                uow.session,
                actor=context,
                action="audit.rollback_probe",
                target=AuditTarget("probe", "p1"),
            )
            raise Boom

    assert await rows_for(app_engine, probe) == []


async def test_the_recorder_joins_the_callers_transaction(
    api: Api, app_engine: AsyncEngine
) -> None:
    probe = new_correlation_id()
    context = AuditContext(
        Actor.system(),
        correlation_id=probe,
        ip="2001:db8::1",
        user_agent="u" * 400,
    )
    async with UnitOfWork(api.factory) as uow:
        record_id = await AuditRecorder.record(
            uow.session,
            actor=context,
            action="audit.commit_probe",
            target=AuditTarget("probe"),
            diff={"note": "ok", "password": "fake-password-1", "nested": {"token": "t"}},
        )
        assert await rows_for(app_engine, probe) == []  # not visible until commit

    assert record_id.startswith("aud_")
    record: dict[str, Any] = await only_row(app_engine, probe)
    assert record["diff"] == {
        "note": "ok",
        "password": "[redacted]",
        "nested": {"token": "[redacted]"},
    }
    assert len(record["user_agent"]) == 256
    assert record["ip"] == "2001:db8::1"
    assert (record["actor_type"], record["actor_id"], record["project_id"]) == (
        "system",
        None,
        None,
    )
