"""Project members API: list/add/change/remove, owner protections, API keys excluded, audit."""

from contextlib import aclosing

from sqlalchemy.ext.asyncio import AsyncEngine

from financial_hub.modules.identity.application.api import Role
from tests.integration.audit.helpers import only_row, tagged
from tests.integration.identity.support import Api, csrf, logged_in
from tests.integration.projects.helpers import bearer, create_key, create_project, member


def url(project_id: str, operator_id: str | None = None) -> str:
    base = f"/v1/projects/{project_id}/members"
    return base if operator_id is None else f"{base}/{operator_id}"


async def test_every_role_can_list_members(api: Api) -> None:
    owner = await api.operator(display_name="Olivia")
    project = await create_project(api, owner)
    for role in Role:
        _, client, _ = await member(api, project["id"], role)
        async with aclosing(client):
            response = await client.get(url(project["id"]))
        assert response.status_code == 200, role
        rows = response.json()["data"]
        first = next(r for r in rows if r["operator_id"] == owner.id)
        assert first["role"] == "owner" and first["email"] == owner.email
        assert first["display_name"] == "Olivia" and first["added_at"]


async def test_admin_adds_changes_and_removes_a_member(api: Api, app_engine: AsyncEngine) -> None:
    owner = await api.operator()
    project = await create_project(api, owner)
    _, admin, token = await member(api, project["id"], Role.ADMIN)
    target = await api.operator()
    async with aclosing(admin):
        cid = tagged(admin)
        added = await admin.post(
            url(project["id"]), json={"email": target.email, "role": "viewer"}, headers=csrf(token)
        )
        assert added.status_code == 201, added.text
        assert added.json()["operator_id"] == target.id and added.json()["role"] == "viewer"
        record = await only_row(app_engine, cid)
        assert (record["action"], record["target_id"]) == ("member.added", target.id)
        assert record["project_id"] is not None

        cid = tagged(admin)
        changed = await admin.patch(
            url(project["id"], target.id), json={"role": "finance"}, headers=csrf(token)
        )
        assert changed.status_code == 200 and changed.json()["role"] == "finance"
        assert (await only_row(app_engine, cid))["action"] == "member.role_changed"

        cid = tagged(admin)
        removed = await admin.delete(url(project["id"], target.id), headers=csrf(token))
        assert removed.status_code == 204
        assert (await only_row(app_engine, cid))["action"] == "member.removed"

        listing = await admin.get(url(project["id"]))
        assert target.id not in [r["operator_id"] for r in listing.json()["data"]]
        again = await admin.delete(url(project["id"], target.id), headers=csrf(token))
        assert again.status_code == 404


async def test_non_managers_cannot_write(api: Api) -> None:
    owner = await api.operator()
    project = await create_project(api, owner)
    target = await api.operator()
    for role in (Role.FINANCE, Role.VIEWER):
        _, client, token = await member(api, project["id"], role)
        async with aclosing(client):
            added = await client.post(
                url(project["id"]),
                json={"email": target.email, "role": "viewer"},
                headers=csrf(token),
            )
            patched = await client.patch(
                url(project["id"], owner.id), json={"role": "viewer"}, headers=csrf(token)
            )
            deleted = await client.delete(url(project["id"], owner.id), headers=csrf(token))
        assert (added.status_code, patched.status_code, deleted.status_code) == (403, 403, 403)


async def test_duplicate_unknown_and_invalid_input(api: Api) -> None:
    owner = await api.operator()
    project = await create_project(api, owner)
    client, token = await logged_in(api, owner)
    async with aclosing(client):
        duplicate = await client.post(
            url(project["id"]), json={"email": owner.email, "role": "viewer"}, headers=csrf(token)
        )
        unknown = await client.post(
            url(project["id"]),
            json={"email": "nobody-here@example.test", "role": "viewer"},
            headers=csrf(token),
        )
        bad_role = await client.post(
            url(project["id"]), json={"email": owner.email, "role": "emperor"}, headers=csrf(token)
        )
        bad_id = await client.delete(url(project["id"], "nope"), headers=csrf(token))
        stranger = await api.operator()
        not_member = await client.patch(
            url(project["id"], stranger.id), json={"role": "viewer"}, headers=csrf(token)
        )

    assert duplicate.status_code == 409
    assert unknown.status_code == 422
    assert unknown.json()["errors"][0]["pointer"] == "/email"
    assert bad_role.status_code == 422
    assert bad_id.status_code == 404
    assert not_member.status_code == 404


async def test_the_last_owner_cannot_be_demoted_or_removed(api: Api) -> None:
    owner = await api.operator()
    project = await create_project(api, owner)
    client, token = await logged_in(api, owner)
    async with aclosing(client):
        demote = await client.patch(
            url(project["id"], owner.id), json={"role": "admin"}, headers=csrf(token)
        )
        remove = await client.delete(url(project["id"], owner.id), headers=csrf(token))
        second = await api.operator()
        added = await client.post(
            url(project["id"]), json={"email": second.email, "role": "owner"}, headers=csrf(token)
        )
        demoted_now = await client.patch(
            url(project["id"], owner.id), json={"role": "admin"}, headers=csrf(token)
        )

    assert demote.status_code == 409 and remove.status_code == 409
    assert added.status_code == 201
    assert demoted_now.status_code == 200


async def test_only_an_owner_can_grant_revoke_or_touch_owners(api: Api) -> None:
    owner = await api.operator()
    project = await create_project(api, owner)
    _, admin, token = await member(api, project["id"], Role.ADMIN)
    candidate = await api.operator()
    async with aclosing(admin):
        grant = await admin.post(
            url(project["id"]),
            json={"email": candidate.email, "role": "owner"},
            headers=csrf(token),
        )
        demote = await admin.patch(
            url(project["id"], owner.id), json={"role": "viewer"}, headers=csrf(token)
        )
        remove = await admin.delete(url(project["id"], owner.id), headers=csrf(token))
    assert (grant.status_code, demote.status_code, remove.status_code) == (403, 403, 403)


async def test_api_keys_cannot_use_the_members_api(api: Api) -> None:
    owner = await api.operator()
    project = await create_project(api, owner)
    client, token = await logged_in(api, owner)
    async with aclosing(client):
        key = await create_key(client, token, project["id"], scopes=["projects:read"])
    async with api.client() as keyed:
        listing = await keyed.get(url(project["id"]), headers=bearer(key["key"]))
        added = await keyed.post(
            url(project["id"]),
            json={"email": owner.email, "role": "viewer"},
            headers=bearer(key["key"]),
        )
    assert listing.status_code == 403
    assert added.status_code == 403


async def test_members_of_another_project_are_not_visible(api: Api) -> None:
    owner = await api.operator()
    other_owner = await api.operator()
    await create_project(api, owner)
    foreign = await create_project(api, other_owner)
    client, _ = await logged_in(api, owner)
    async with aclosing(client):
        response = await client.get(url(foreign["id"]))
    assert response.status_code == 404


async def test_add_honours_the_idempotency_key(api: Api) -> None:
    owner = await api.operator()
    project = await create_project(api, owner)
    target = await api.operator()
    client, token = await logged_in(api, owner)
    headers = {**csrf(token), "Idempotency-Key": "fake-member-add-1"}
    async with aclosing(client):
        first = await client.post(
            url(project["id"]), json={"email": target.email, "role": "viewer"}, headers=headers
        )
        replay = await client.post(
            url(project["id"]), json={"email": target.email, "role": "viewer"}, headers=headers
        )
    assert first.status_code == 201 and replay.status_code == 201
    assert replay.json() == first.json()
