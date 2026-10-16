"""Builders for the audit suites (real Postgres, rows read back as ``fh_app``)."""

import secrets
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

CORRELATION_HEADER = "X-Correlation-Id"
SELECT_BY_CORRELATION = (
    "SELECT id, project_id, actor_type, actor_id, action, target_type, target_id, diff, "
    "correlation_id, request_id, host(ip) AS ip, user_agent, occurred_at "
    "FROM audit_records WHERE correlation_id = :c ORDER BY occurred_at, id"
)


def new_correlation_id() -> str:
    return f"audit-{secrets.token_hex(8)}"


def tagged(client: httpx.AsyncClient) -> str:
    """Make every later request of ``client`` carry a fresh correlation id; return it."""
    correlation_id = new_correlation_id()
    client.headers[CORRELATION_HEADER] = correlation_id
    return correlation_id


async def rows_for(engine: AsyncEngine, correlation_id: str) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(SELECT_BY_CORRELATION),
            {"c": correlation_id},
        )
        return [dict(row._mapping) for row in result]  # pyright: ignore[reportPrivateUsage]


async def only_row(engine: AsyncEngine, correlation_id: str) -> dict[str, Any]:
    rows = await rows_for(engine, correlation_id)
    assert len(rows) == 1, [row["action"] for row in rows]
    return rows[0]


async def dump_table(engine: AsyncEngine) -> str:
    """Every audit row as one string, for 'no secret anywhere' assertions."""
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT t::text FROM audit_records t"))
        return "\n".join(value for (value,) in result)
