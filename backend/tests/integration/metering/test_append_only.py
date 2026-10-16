"""usage_events is append-only; billable_metrics changes only by archiving (database level)."""

from typing import Any
from uuid import uuid4

import psycopg.errors as pg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from financial_hub.shared.ids import IdPrefix, parse_id
from tests.integration.identity.support import Api, csrf
from tests.integration.metering.helpers import meter

USAGE_STATEMENTS = [
    "UPDATE usage_events SET value = 1 WHERE project_id = :p",
    "UPDATE usage_events SET properties = '{}'::jsonb WHERE project_id = :p",
    "DELETE FROM usage_events WHERE project_id = :p",
    "TRUNCATE usage_events",
]
METRIC_STATEMENTS = [
    "UPDATE billable_metrics SET aggregation = 'count' WHERE project_id = :p",
    "UPDATE billable_metrics SET field = 'other' WHERE project_id = :p",
    "UPDATE billable_metrics SET code = 'renamed' WHERE project_id = :p",
    "DELETE FROM billable_metrics WHERE project_id = :p",
]


async def run_failing(
    engine: AsyncEngine, sql: str, expected: type[Exception], params: dict[str, Any]
) -> None:
    async with engine.connect() as conn:
        await conn.begin()
        with pytest.raises(DBAPIError) as info:
            await conn.execute(text(sql), params)
        await conn.rollback()
    assert isinstance(info.value.orig, expected), info.value.orig


@pytest.mark.parametrize("sql", USAGE_STATEMENTS)
async def test_runtime_role_cannot_change_delete_or_truncate_usage(
    api: Api, app_engine: AsyncEngine, sql: str
) -> None:
    async with meter(api) as m:
        await m.metric("api_calls")
        assert (await m.ingest(m.event("api_calls"))).status_code == 201
        await run_failing(app_engine, sql, pg.InsufficientPrivilege, {"p": m.project_uuid})


@pytest.mark.parametrize("sql", USAGE_STATEMENTS)
async def test_the_owner_is_stopped_by_the_triggers(
    api: Api, owner_engine: AsyncEngine, app_engine: AsyncEngine, sql: str
) -> None:
    async with meter(api) as m:
        await m.metric("api_calls")
        assert (await m.ingest(m.event("api_calls"))).status_code == 201
        params = {} if sql.startswith("TRUNCATE") else {"p": m.project_uuid}
        await run_failing(owner_engine, sql, pg.RestrictViolation, params)
        async with app_engine.connect() as conn:
            kept = (
                await conn.execute(
                    text("SELECT count(*) FROM usage_events WHERE project_id = :p"),
                    {"p": m.project_uuid},
                )
            ).scalar_one()
    assert kept == 1


@pytest.mark.parametrize("sql", METRIC_STATEMENTS)
async def test_a_metric_definition_cannot_be_changed_or_deleted(
    api: Api, app_engine: AsyncEngine, sql: str
) -> None:
    async with meter(api) as m:
        await m.metric("api_calls")
        await run_failing(app_engine, sql, pg.InsufficientPrivilege, {"p": m.project_uuid})


async def test_an_archived_metric_cannot_be_reactivated_even_by_the_owner(
    api: Api, owner_engine: AsyncEngine
) -> None:
    async with meter(api) as m:
        await m.metric("api_calls")
        await m.owner_client.post(f"{m.metrics_path}/api_calls/archive", headers=csrf(m.csrf_token))
        await run_failing(
            owner_engine,
            "UPDATE billable_metrics SET status = 'active', archived_at = NULL "
            "WHERE project_id = :p",
            pg.RestrictViolation,
            {"p": m.project_uuid},
        )


async def test_an_event_cannot_name_a_customer_of_another_project(
    api: Api, app_engine: AsyncEngine
) -> None:
    async with meter(api) as a, meter(api) as b:
        await a.metric("api_calls")
        foreign = parse_id(b.customer_id, IdPrefix.CUSTOMER)
        async with app_engine.connect() as conn:
            await conn.begin()
            with pytest.raises(DBAPIError) as info:
                await conn.execute(
                    text(
                        "INSERT INTO usage_events (id, project_id, customer_id, metric_code, "
                        "transaction_id, occurred_at, properties, ingested_at) VALUES "
                        "(:id, :p, :c, 'api_calls', 'forged-1', now(), '{}'::jsonb, now())"
                    ),
                    {"id": uuid4(), "p": a.project_uuid, "c": foreign},
                )
            await conn.rollback()

    assert isinstance(info.value.orig, pg.IntegrityError), info.value.orig
