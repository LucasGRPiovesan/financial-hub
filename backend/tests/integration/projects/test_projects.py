"""/v1/projects and the project-scoped RBAC of ``require()`` (403 member, 404 non-member)."""

from contextlib import aclosing

import pytest

from financial_hub.modules.identity.application.api import Role
from tests.integration.identity.support import Api, Operator, csrf, logged_in, unique_slug


async def create_project(api: Api, owner: Operator, name: str = "Project") -> dict[str, str]:
    client, csrf_token = await logged_in(api, owner)
    async with aclosing(client):
        response = await client.post(
            "/v1/projects", json={"slug": unique_slug(), "name": name}, headers=csrf(csrf_token)
        )
    assert response.status_code == 201, response.text
    return response.json()


async def test_creator_becomes_owner_and_sees_the_project(api: Api) -> None:
    owner = await api.operator()
    client, csrf_token = await logged_in(api, owner)
    slug = unique_slug()
    async with aclosing(client):
        created = await client.post(
            "/v1/projects", json={"slug": slug, "name": " Acme Store "}, headers=csrf(csrf_token)
        )
        listed = await client.get("/v1/projects")
        fetched = await client.get(f"/v1/projects/{created.json()['id']}")
        me = await client.get("/v1/auth/me")

    assert created.status_code == 201
    project = created.json()
    assert project["id"].startswith("prj_")
    assert (project["slug"], project["name"], project["status"]) == (slug, "Acme Store", "active")
    assert listed.json() == {"data": [project], "next_cursor": None, "has_more": False}
    assert fetched.json() == project
    assert me.json()["memberships"] == [
        {"project_id": project["id"], "project_name": "Acme Store", "role": "owner"}
    ]


async def test_listing_shows_only_the_callers_projects(api: Api) -> None:
    alice, bob = await api.operator(), await api.operator()
    mine = await create_project(api, alice, "Mine")
    await create_project(api, bob, "Not mine")
    client, _ = await logged_in(api, alice)
    async with aclosing(client):
        listed = await client.get("/v1/projects")

    assert [p["id"] for p in listed.json()["data"]] == [mine["id"]]


async def test_listing_paginates(api: Api) -> None:
    owner = await api.operator()
    ids = [(await create_project(api, owner, f"P{i}"))["id"] for i in range(3)]
    client, _ = await logged_in(api, owner)
    async with aclosing(client):
        first = (await client.get("/v1/projects", params={"limit": 2})).json()
        second = (
            await client.get("/v1/projects", params={"limit": 2, "cursor": first["next_cursor"]})
        ).json()

    assert first["has_more"] is True
    assert second["has_more"] is False
    assert sorted(p["id"] for p in first["data"] + second["data"]) == sorted(ids)


async def test_duplicate_slug_is_409(api: Api) -> None:
    owner = await api.operator()
    client, csrf_token = await logged_in(api, owner)
    slug = unique_slug()
    async with aclosing(client):
        first = await client.post(
            "/v1/projects", json={"slug": slug, "name": "One"}, headers=csrf(csrf_token)
        )
        second = await client.post(
            "/v1/projects", json={"slug": slug, "name": "Two"}, headers=csrf(csrf_token)
        )

    assert first.status_code == 201
    assert second.status_code == 409
    assert [e["pointer"] for e in second.json()["errors"]] == ["/slug"]


@pytest.mark.parametrize("slug", ["Upper", "ab", "-lead", "trail-", "has space", "x" * 51])
async def test_invalid_slugs_are_422(api: Api, slug: str) -> None:
    owner = await api.operator()
    client, csrf_token = await logged_in(api, owner)
    async with aclosing(client):
        response = await client.post(
            "/v1/projects", json={"slug": slug, "name": "Bad"}, headers=csrf(csrf_token)
        )

    assert response.status_code == 422
    assert [e["pointer"] for e in response.json()["errors"]] == ["/slug"]


@pytest.mark.parametrize(
    ("role", "can_read", "can_manage"),
    [
        (Role.OWNER, True, True),
        (Role.ADMIN, True, True),
        (Role.FINANCE, True, False),
        (Role.VIEWER, True, False),
    ],
)
async def test_role_matrix_on_project_routes(
    api: Api, role: Role, can_read: bool, can_manage: bool
) -> None:
    owner, member = await api.operator(), await api.operator()
    project = await create_project(api, owner)
    await api.grant(member, project["id"], role)
    client, csrf_token = await logged_in(api, member)
    async with aclosing(client):
        read = await client.get(f"/v1/projects/{project['id']}")
        manage = await client.patch(
            f"/v1/projects/{project['id']}", json={"name": "Renamed"}, headers=csrf(csrf_token)
        )

    assert read.status_code == (200 if can_read else 403)
    assert manage.status_code == (200 if can_manage else 403)
    if can_manage:
        assert manage.json()["name"] == "Renamed"
    else:
        assert manage.json()["detail"] == "Missing permission projects:manage."


async def test_non_members_get_404_not_403(api: Api) -> None:
    owner, outsider = await api.operator(), await api.operator()
    project = await create_project(api, owner)
    client, csrf_token = await logged_in(api, outsider)
    async with aclosing(client):
        read = await client.get(f"/v1/projects/{project['id']}")
        manage = await client.patch(
            f"/v1/projects/{project['id']}", json={"name": "Hijack"}, headers=csrf(csrf_token)
        )
        malformed = await client.get("/v1/projects/not-a-project-id")
        wrong_prefix = await client.get(f"/v1/projects/{owner.id}")

    assert read.status_code == manage.status_code == 404
    assert malformed.status_code == wrong_prefix.status_code == 404
    assert read.json()["detail"] == "Project not found."


async def test_project_routes_require_a_session(api: Api) -> None:
    owner = await api.operator()
    project = await create_project(api, owner)
    async with api.client() as client:
        listed = await client.get("/v1/projects")
        read = await client.get(f"/v1/projects/{project['id']}")
        created = await client.post("/v1/projects", json={"slug": unique_slug(), "name": "X"})

    assert listed.status_code == read.status_code == created.status_code == 401
