"""audit_records is append-only: ``fh_app`` has INSERT+SELECT only, and triggers stop the owner."""

from typing import Any
from uuid import UUID, uuid4

import psycopg.errors as pg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

INSERT = (
    "INSERT INTO audit_records (id, actor_type, action, target_type) "
    "VALUES (:id, 'system', 'audit.test_probe', 'probe')"
)
STATEMENTS = [
    "UPDATE audit_records SET action = 'audit.tampered' WHERE id = :id",
    "UPDATE audit_records SET diff = '{}'::jsonb WHERE id = :id",
    "DELETE FROM audit_records WHERE id = :id",
    "TRUNCATE audit_records",
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


@pytest.fixture
async def record_id(app_engine: AsyncEngine) -> UUID:
    """One committed row for the statements below to (fail to) change."""
    identifier = uuid4()
    async with app_engine.begin() as conn:
        await conn.execute(text(INSERT), {"id": identifier})
    return identifier


@pytest.mark.parametrize("sql", STATEMENTS)
async def test_runtime_role_cannot_change_delete_or_truncate(
    app_engine: AsyncEngine, record_id: UUID, sql: str
) -> None:
    await run_failing(app_engine, sql, pg.InsufficientPrivilege, {"id": record_id})


@pytest.mark.parametrize("sql", STATEMENTS)
async def test_the_owner_is_stopped_by_the_triggers(
    app_engine: AsyncEngine, owner_engine: AsyncEngine, record_id: UUID, sql: str
) -> None:
    params = {} if sql.startswith("TRUNCATE") else {"id": record_id}
    await run_failing(owner_engine, sql, pg.RestrictViolation, params)
    async with app_engine.connect() as conn:
        action = (
            await conn.execute(
                text("SELECT action FROM audit_records WHERE id = :id"), {"id": record_id}
            )
        ).scalar_one()
    assert action == "audit.test_probe"


async def test_runtime_role_can_insert_and_read(app_engine: AsyncEngine) -> None:
    identifier = uuid4()
    async with app_engine.begin() as conn:
        await conn.execute(text(INSERT), {"id": identifier})
    async with app_engine.connect() as conn:
        found = (
            await conn.execute(
                text("SELECT count(*) FROM audit_records WHERE id = :id"), {"id": identifier}
            )
        ).scalar_one()
    assert found == 1


@pytest.mark.parametrize(
    ("actor_type", "action"),
    [
        ("robot", "audit.test_probe"),  # actor_type outside operator|api_key|system
        ("system", "NotDotted"),  # action must be a dotted lowercase name
        ("system", "single"),
    ],
)
async def test_the_schema_rejects_malformed_records(
    app_engine: AsyncEngine, actor_type: str, action: str
) -> None:
    sql = (
        "INSERT INTO audit_records (id, actor_type, action, target_type) "
        "VALUES (:id, :actor_type, :action, 'probe')"
    )
    params = {"id": uuid4(), "actor_type": actor_type, "action": action}
    await run_failing(app_engine, sql, pg.CheckViolation, params)


async def test_a_user_agent_over_256_characters_is_rejected_by_the_schema(
    app_engine: AsyncEngine,
) -> None:
    sql = (
        "INSERT INTO audit_records (id, actor_type, action, target_type, user_agent) "
        "VALUES (:id, 'system', 'audit.test_probe', 'probe', repeat('x', 257))"
    )
    await run_failing(app_engine, sql, pg.CheckViolation, {"id": uuid4()})
