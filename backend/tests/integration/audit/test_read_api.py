"""GET /v1/projects/{id}/audit-records: pagination, filters, isolation and who may read."""

from contextlib import aclosing
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from financial_hub.modules.identity.application.api import Role
from financial_hub.shared.ids import IdPrefix, parse_id
from tests.integration.identity.support import Api, csrf, logged_in
from tests.integration.projects.helpers import bearer, create_key, create_project, member


def url(project_id: str) -> str:
    return f"/v1/projects/{project_id}/audit-records"


async def seeded(api: Api) -> tuple[dict[str, str], Any, str, str]:
    """A project with 3 audit records: project.created, member.added and api_key.created."""
    owner = await api.operator()
    project = await create_project(api, owner)
    client, token = await logged_in(api, owner)
    async with aclosing(client):
        key = await create_key(client, token, project["id"])
    return project, owner, key["id"], key["key"]


async def test_lists_newest_first_and_paginates_without_gaps(api: Api) -> None:
    project, owner, key_id, _ = await seeded(api)
    client, _ = await logged_in(api, owner)
    async with aclosing(client):
        everything = await client.get(url(project["id"]))
        first = await client.get(url(project["id"]), params={"limit": 2})
        assert first.status_code == 200
        body = first.json()
        assert body["has_more"] is True
        second = await client.get(
            url(project["id"]), params={"limit": 2, "cursor": body["next_cursor"]}
        )

    full = everything.json()["data"]
    assert [row["action"] for row in full] == ["api_key.created", "member.added", "project.created"]
    assert full[0]["target_id"] == key_id
    paged = body["data"] + second.json()["data"]
    assert [row["id"] for row in paged] == [row["id"] for row in full]
    assert second.json()["has_more"] is False
    assert second.json()["next_cursor"] is None
    assert all(row["id"].startswith("aud_") for row in full)
    assert all(row["project_id"] == project["id"] for row in full)
    occurred = [row["occurred_at"] for row in full]
    assert occurred == sorted(occurred, reverse=True)


async def test_filters(api: Api) -> None:
    project, owner, key_id, _ = await seeded(api)
    client, _ = await logged_in(api, owner)
    async with aclosing(client):

        async def actions(**params: str) -> list[str]:
            response = await client.get(url(project["id"]), params=params)
            assert response.status_code == 200, response.text
            return [row["action"] for row in response.json()["data"]]

        assert await actions(action="project.created") == ["project.created"]
        assert await actions(target_type="api_key") == ["api_key.created"]
        assert await actions(target_type="api_key", target_id=key_id) == ["api_key.created"]
        assert await actions(target_id="key_" + "0" * 26) == []
        assert sorted(await actions(actor_id=owner.id)) == [
            "api_key.created",
            "member.added",
            "project.created",
        ]
        assert await actions(actor_id="opr_nobody") == []

        everything = (await client.get(url(project["id"]))).json()["data"]
        newest = datetime.fromisoformat(everything[0]["occurred_at"])
        oldest = datetime.fromisoformat(everything[-1]["occurred_at"])
        assert await actions(occurred_from=(newest + timedelta(seconds=1)).isoformat()) == []
        assert await actions(occurred_to=oldest.isoformat()) == []  # exclusive upper bound
        assert len(await actions(occurred_from=oldest.isoformat())) == 3
        assert len(await actions(occurred_to=(newest + timedelta(seconds=1)).isoformat())) == 3


async def test_records_of_another_project_are_a_404_not_a_leak(api: Api) -> None:
    project_a, owner_a, _, _ = await seeded(api)
    project_b, owner_b, _, _ = await seeded(api)
    client, _ = await logged_in(api, owner_a)
    async with aclosing(client):
        foreign = await client.get(url(project_b["id"]))
        own = await client.get(url(project_a["id"]))
    assert foreign.status_code == 404
    assert own.status_code == 200
    assert {row["project_id"] for row in own.json()["data"]} == {project_a["id"]}
    assert project_b["id"] not in own.text
    assert owner_b.id not in own.text


@pytest.mark.parametrize(
    ("role", "expected"),
    [(Role.OWNER, 200), (Role.ADMIN, 200), (Role.FINANCE, 200), (Role.VIEWER, 403)],
)
async def test_only_roles_with_audit_read_may_list(api: Api, role: Role, expected: int) -> None:
    owner = await api.operator()
    project = await create_project(api, owner)
    _, client, _ = await member(api, project["id"], role)
    async with aclosing(client):
        response = await client.get(url(project["id"]))
    assert response.status_code == expected


async def test_an_api_key_can_never_read_audit(api: Api) -> None:
    project, _, _, key = await seeded(api)
    async with api.client() as client:
        response = await client.get(url(project["id"]), headers=bearer(key))
    assert response.status_code in (401, 403)


async def test_an_anonymous_caller_is_rejected(api: Api) -> None:
    project, _, _, _ = await seeded(api)
    async with api.client() as client:
        response = await client.get(url(project["id"]))
    assert response.status_code == 401


async def test_a_malformed_cursor_is_a_problem_not_a_500(api: Api) -> None:
    project, owner, _, _ = await seeded(api)
    client, _ = await logged_in(api, owner)
    async with aclosing(client):
        response = await client.get(url(project["id"]), params={"cursor": "not-a-cursor"})
    assert response.status_code == 422
    assert response.json()["status"] == 422


async def test_the_listing_only_reads(api: Api, app_engine: AsyncEngine) -> None:
    project, owner, _, _ = await seeded(api)

    async def count() -> int:
        async with app_engine.connect() as conn:
            return (
                await conn.execute(
                    text("SELECT count(*) FROM audit_records WHERE project_id = :p"),
                    {"p": parse_id(project["id"], IdPrefix.PROJECT)},
                )
            ).scalar_one()

    before = await count()
    client, token = await logged_in(api, owner)
    async with aclosing(client):
        assert (await client.get(url(project["id"]))).status_code == 200
        assert (await client.get(url(project["id"]))).status_code == 200
        assert (await client.post(url(project["id"]), headers=csrf(token))).status_code == 405
        assert (await client.delete(url(project["id"]), headers=csrf(token))).status_code == 405
    assert await count() == before
