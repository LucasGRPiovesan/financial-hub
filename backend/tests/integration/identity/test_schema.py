"""The Core table declarations match what migration ``0003_identity`` actually created."""

import pytest
from sqlalchemy import Table, text
from sqlalchemy.ext.asyncio import AsyncEngine

from financial_hub.modules.identity.infrastructure.tables import (
    login_throttle,
    operators,
    project_memberships,
    sessions,
)
from financial_hub.modules.projects.infrastructure.tables import projects

COLUMNS = text(
    "SELECT column_name, is_nullable FROM information_schema.columns "
    "WHERE table_schema = 'public' AND table_name = :table"
)


@pytest.mark.parametrize(
    "table", [operators, sessions, project_memberships, login_throttle, projects], ids=str
)
async def test_declared_columns_match_the_database(owner_engine: AsyncEngine, table: Table) -> None:
    async with owner_engine.connect() as connection:
        rows = (await connection.execute(COLUMNS, {"table": table.name})).all()

    in_database = {name: nullable == "YES" for name, nullable in rows}
    declared = {column.name: bool(column.nullable) for column in table.columns}
    assert declared == in_database
