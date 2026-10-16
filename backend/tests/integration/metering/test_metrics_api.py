"""Billable metrics through the operator API: lifecycle, roles, validation and audit."""

from contextlib import aclosing

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from financial_hub.modules.identity.application.api import Role
from tests.integration.identity.support import Api, csrf
from tests.integration.metering.helpers import meter
from tests.integration.projects.helpers import member


async def test_create_list_get_and_archive(api: Api) -> None:
    async with meter(api) as m:
        created = await m.metric("api_calls")
        sum_metric = await m.metric("gigabytes", "sum", "gb")
        listed = await m.owner_client.get(m.metrics_path)
        fetched = await m.owner_client.get(f"{m.metrics_path}/gigabytes")
        archived = await m.owner_client.post(
            f"{m.metrics_path}/gigabytes/archive", headers=csrf(m.csrf_token)
        )
        again = await m.owner_client.post(
            f"{m.metrics_path}/gigabytes/archive", headers=csrf(m.csrf_token)
        )
        only_active = await m.owner_client.get(m.metrics_path, params={"status": "active"})

    assert (created["code"], created["aggregation"], created["field"]) == (
        "api_calls",
        "count",
        None,
    )
    assert created["status"] == "active" and created["archived_at"] is None
    assert (sum_metric["aggregation"], sum_metric["field"]) == ("sum", "gb")
    assert {metric["code"] for metric in listed.json()["data"]} == {"api_calls", "gigabytes"}
    assert fetched.json() == sum_metric
    assert archived.status_code == 200, archived.text
    assert archived.json()["status"] == "archived" and archived.json()["archived_at"] is not None
    assert again.status_code == 200 and again.json() == archived.json()  # idempotent
    assert [metric["code"] for metric in only_active.json()["data"]] == ["api_calls"]


async def test_duplicate_code_is_a_conflict_and_the_definition_is_untouched(api: Api) -> None:
    async with meter(api) as m:
        first = await m.metric("api_calls")
        again = await m.owner_client.post(
            m.metrics_path,
            json={"code": "api_calls", "name": "Other", "aggregation": "sum", "field": "x"},
            headers=csrf(m.csrf_token),
        )
        fetched = await m.owner_client.get(f"{m.metrics_path}/api_calls")

    assert again.status_code == 409, again.text
    assert again.json()["type"].endswith("/metric-code-taken")
    assert fetched.json() == first


async def test_the_same_code_can_exist_in_two_projects(api: Api) -> None:
    async with meter(api) as one, meter(api) as two:
        await one.metric("api_calls")
        await two.metric("api_calls")
        other = await one.owner_client.get(f"/v1/projects/{two.project_id}/metrics")

    assert other.status_code in (403, 404)


async def test_invalid_definitions_are_rejected_with_field_pointers(api: Api) -> None:
    async with meter(api) as m:
        bodies = {
            "/aggregation": {"code": "a", "name": "A", "aggregation": "avg", "field": "x"},
            "/field": {"code": "b", "name": "B", "aggregation": "sum"},
            "/code": {"code": "Bad-Code", "name": "C", "aggregation": "count"},
        }
        responses = {
            pointer: await m.owner_client.post(
                m.metrics_path, json=body, headers=csrf(m.csrf_token)
            )
            for pointer, body in bodies.items()
        }
        count_with_field = await m.owner_client.post(
            m.metrics_path,
            json={"code": "d", "name": "D", "aggregation": "count", "field": "x"},
            headers=csrf(m.csrf_token),
        )
        listed = await m.owner_client.get(m.metrics_path)

    for pointer, response in responses.items():
        assert response.status_code == 422, response.text
        assert pointer in [error["pointer"] for error in response.json()["errors"]]
    assert count_with_field.status_code == 422
    assert listed.json()["data"] == []


async def test_unknown_metric_is_404(api: Api) -> None:
    async with meter(api) as m:
        fetched = await m.owner_client.get(f"{m.metrics_path}/nope")
        archived = await m.owner_client.post(
            f"{m.metrics_path}/nope/archive", headers=csrf(m.csrf_token)
        )

    assert fetched.status_code == 404
    assert archived.status_code == 404


async def test_roles_viewer_reads_but_cannot_manage(api: Api) -> None:
    async with meter(api) as m:
        await m.metric("api_calls")
        for role, can_manage in ((Role.VIEWER, False), (Role.ADMIN, True), (Role.FINANCE, True)):
            _, client, token = await member(api, m.project_id, role)
            async with aclosing(client):
                read = await client.get(m.metrics_path)
                create = await client.post(
                    m.metrics_path,
                    json={"code": f"c_{role.value}", "name": "C", "aggregation": "count"},
                    headers=csrf(token),
                )
                archive = await client.post(
                    f"{m.metrics_path}/api_calls/archive", headers=csrf(token)
                )
            assert read.status_code == 200, (role, read.text)
            if can_manage:
                assert create.status_code == 201, (role, create.text)
                assert archive.status_code == 200, (role, archive.text)
            else:
                assert create.status_code == 403, (role, create.text)
                assert archive.status_code == 403, (role, archive.text)
        listed = await m.owner_client.get(m.metrics_path)

    codes = {metric["code"] for metric in listed.json()["data"]}
    assert codes == {"api_calls", "c_admin", "c_finance"}


async def test_an_api_key_cannot_use_the_operator_metrics_routes(api: Api) -> None:
    async with meter(api) as m, api.client() as client:
        read = await client.get(m.metrics_path, headers={"Authorization": f"Bearer {m.key}"})

    assert read.status_code in (401, 403)


async def test_metric_changes_are_audited(api: Api, app_engine: AsyncEngine) -> None:
    async with meter(api) as m:
        await m.metric("gigabytes", "sum", "gb")
        await m.owner_client.post(f"{m.metrics_path}/gigabytes/archive", headers=csrf(m.csrf_token))
        await m.owner_client.post(f"{m.metrics_path}/gigabytes/archive", headers=csrf(m.csrf_token))
        async with app_engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT action, target_type, target_id, actor_type, diff "
                        "FROM audit_records WHERE project_id = :p "
                        "AND target_type = 'billable_metric' "
                        "ORDER BY occurred_at, id"
                    ),
                    {"p": m.project_uuid},
                )
            ).all()

    assert [row.action for row in rows] == ["metric.created", "metric.archived"]
    assert all(row.target_id == "gigabytes" and row.actor_type == "operator" for row in rows)
    assert rows[0].diff == {"aggregation": "sum", "field": "gb", "name": "Gigabytes"}
