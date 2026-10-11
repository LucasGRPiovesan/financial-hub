"""The database enforces the ledger invariants by itself (ADR-0003 s2-s4), as ``fh_app``.

Raw SQL on purpose: these tests bypass the service to prove that buggy or hostile application code
with the runtime credentials cannot corrupt the ledger.
"""

from collections.abc import Awaitable, Callable
from uuid import UUID

import pytest
from psycopg import errors as pg
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from financial_hub.shared.ids import new_uuid

type Build = Callable[[AsyncConnection], Awaitable[object]]


async def account(
    conn: AsyncConnection,
    project_id: UUID,
    code: str,
    account_type: str = "asset",
    *,
    currency: str = "BRL",
    no_overdraft: bool = False,
) -> UUID:
    account_id = new_uuid()
    await conn.execute(
        text(
            "INSERT INTO ledger_accounts (id, project_id, code, currency, type, no_overdraft) "
            "VALUES (:id, :project_id, :code, :currency, :type, :no_overdraft)"
        ),
        {
            "id": account_id,
            "project_id": project_id,
            "code": code,
            "currency": currency,
            "type": account_type,
            "no_overdraft": no_overdraft,
        },
    )
    return account_id


async def entry(conn: AsyncConnection, project_id: UUID, status: str = "posted") -> UUID:
    entry_id = new_uuid()
    await conn.execute(
        text(
            "INSERT INTO journal_entries (id, project_id, status, idempotency_key, payload_hash, "
            "recipe, effective_at) VALUES (:id, :project_id, :status, :key, :hash, 'test', now())"
        ),
        {
            "id": entry_id,
            "project_id": project_id,
            "status": status,
            "key": str(entry_id),
            "hash": "0" * 64,
        },
    )
    return entry_id


async def posting(
    conn: AsyncConnection,
    entry_id: UUID,
    project_id: UUID,
    account_id: UUID,
    direction: str,
    amount: int,
    currency: str = "BRL",
) -> None:
    await conn.execute(
        text(
            "INSERT INTO ledger_postings (id, entry_id, project_id, account_id, currency, "
            "direction, amount) VALUES (:id, :entry_id, :project_id, :account_id, :currency, "
            ":direction, :amount)"
        ),
        {
            "id": new_uuid(),
            "entry_id": entry_id,
            "project_id": project_id,
            "account_id": account_id,
            "currency": currency,
            "direction": direction,
            "amount": amount,
        },
    )


async def apply(conn: AsyncConnection, entry_id: UUID) -> int:
    result = await conn.execute(text("SELECT ledger_apply(:id)"), {"id": entry_id})
    return result.scalar_one()


async def valid_entry(conn: AsyncConnection, project_id: UUID, amount: int = 100) -> UUID:
    cash = await account(conn, project_id, f"cash:{new_uuid()}")
    revenue = await account(conn, project_id, f"revenue:{new_uuid()}", "income")
    entry_id = await entry(conn, project_id)
    await posting(conn, entry_id, project_id, cash, "debit", amount)
    await posting(conn, entry_id, project_id, revenue, "credit", amount)
    await apply(conn, entry_id)
    return entry_id


async def commit_fails(engine: AsyncEngine, build: Build, error: type[pg.Error]) -> str:
    """Every statement succeeds; the deferred constraint trigger rejects the commit."""
    async with engine.connect() as conn:
        transaction = await conn.begin()
        await build(conn)
        with pytest.raises(DBAPIError) as info:
            await transaction.commit()
    assert isinstance(info.value.orig, error), info.value.orig
    return str(info.value.orig)


async def statement_fails(engine: AsyncEngine, build: Build, error: type[pg.Error]) -> str:
    async with engine.connect() as conn:
        await conn.begin()
        with pytest.raises(DBAPIError) as info:
            await build(conn)
        await conn.rollback()
    assert isinstance(info.value.orig, error), info.value.orig
    return str(info.value.orig)


async def scalar(engine: AsyncEngine, sql: str, **params: object) -> object:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar_one()


# --- the runtime role -------------------------------------------------------------------------


async def test_tests_run_as_restricted_fh_app(app_engine: AsyncEngine) -> None:
    assert await scalar(app_engine, "SELECT current_user") == "fh_app"
    attributes = await scalar(
        app_engine,
        "SELECT ARRAY[rolsuper, rolcreaterole, rolcreatedb, rolinherit, rolbypassrls] "
        "FROM pg_roles WHERE rolname = current_user",
    )
    assert attributes == [False, False, False, False, False]
    owned = await scalar(
        app_engine, "SELECT count(*) FROM pg_class WHERE relowner = 'fh_app'::regrole"
    )
    assert owned == 0


async def test_ledger_functions_are_hardened(owner_engine: AsyncEngine) -> None:
    async with owner_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT p.proname, p.prosecdef, p.proconfig, pg_get_userbyid(p.proowner), "
                    "EXISTS (SELECT 1 FROM aclexplode(coalesce(p.proacl, acldefault('f', "
                    "p.proowner))) a WHERE a.grantee = 0) AS public_execute "
                    "FROM pg_proc p WHERE p.proname LIKE 'ledger%' OR p.proname LIKE 'journal%'"
                )
            )
        ).all()
    by_name = {row[0]: row for row in rows}
    for name in ("ledger_apply", "ledger_lock_accounts"):
        _, security_definer, config, owner, public_execute = by_name[name]
        assert security_definer is True
        assert config == ["search_path=pg_catalog, public, pg_temp"]
        assert owner == "fh_owner"
        assert public_execute is False
    assert all(row[4] is False for row in rows), rows
    assert all(row[3] == "fh_owner" for row in rows), rows


async def test_balanced_applied_entry_commits(app_engine: AsyncEngine, project_id: UUID) -> None:
    async with app_engine.begin() as conn:
        entry_id = await valid_entry(conn, project_id)
        assert await apply(conn, entry_id) == 0  # idempotent: already applied
    total = await scalar(
        app_engine,
        "SELECT sum(posted_dr + posted_cr) FROM ledger_accounts WHERE project_id = :p",
        p=project_id,
    )
    assert total == 200


# --- deferred checks at commit ----------------------------------------------------------------


async def test_unbalanced_entry_fails_at_commit(app_engine: AsyncEngine, project_id: UUID) -> None:
    async def build(conn: AsyncConnection) -> None:
        cash = await account(conn, project_id, "cash")
        revenue = await account(conn, project_id, "revenue", "income")
        entry_id = await entry(conn, project_id)
        await posting(conn, entry_id, project_id, cash, "debit", 100)
        await posting(conn, entry_id, project_id, revenue, "credit", 50)
        await apply(conn, entry_id)

    assert "not balanced" in await commit_fails(app_engine, build, pg.CheckViolation)


async def test_single_posting_entry_fails_at_commit(
    app_engine: AsyncEngine, project_id: UUID
) -> None:
    async def build(conn: AsyncConnection) -> None:
        cash = await account(conn, project_id, "cash")
        entry_id = await entry(conn, project_id)
        await posting(conn, entry_id, project_id, cash, "debit", 100)
        await apply(conn, entry_id)

    assert "at least 2" in await commit_fails(app_engine, build, pg.CheckViolation)


async def test_entry_without_postings_fails_at_commit(
    app_engine: AsyncEngine, project_id: UUID
) -> None:
    async def build(conn: AsyncConnection) -> None:
        await apply(conn, await entry(conn, project_id))

    assert "at least 2" in await commit_fails(app_engine, build, pg.CheckViolation)


async def test_unapplied_entry_fails_at_commit(app_engine: AsyncEngine, project_id: UUID) -> None:
    async def build(conn: AsyncConnection) -> None:
        cash = await account(conn, project_id, "cash")
        revenue = await account(conn, project_id, "revenue", "income")
        entry_id = await entry(conn, project_id)
        await posting(conn, entry_id, project_id, cash, "debit", 100)
        await posting(conn, entry_id, project_id, revenue, "credit", 100)

    assert "not applied" in await commit_fails(app_engine, build, pg.CheckViolation)


async def test_entry_balanced_only_across_currencies_fails_at_commit(
    app_engine: AsyncEngine, project_id: UUID
) -> None:
    async def build(conn: AsyncConnection) -> None:
        cash = await account(conn, project_id, "cash")
        revenue = await account(conn, project_id, "revenue", "income", currency="USD")
        entry_id = await entry(conn, project_id)
        await posting(conn, entry_id, project_id, cash, "debit", 100)
        await posting(conn, entry_id, project_id, revenue, "credit", 100, "USD")
        await apply(conn, entry_id)

    assert "per currency" in await commit_fails(app_engine, build, pg.CheckViolation)


async def test_no_overdraft_enforced_at_commit(app_engine: AsyncEngine, project_id: UUID) -> None:
    async def build(conn: AsyncConnection) -> None:
        wallet = await account(conn, project_id, "wallet", "liability", no_overdraft=True)
        revenue = await account(conn, project_id, "revenue", "income")
        entry_id = await entry(conn, project_id)
        await posting(conn, entry_id, project_id, wallet, "debit", 10)
        await posting(conn, entry_id, project_id, revenue, "credit", 10)
        await apply(conn, entry_id)

    assert "no_overdraft" in await commit_fails(app_engine, build, pg.CheckViolation)


async def test_posting_in_another_currency_than_its_account_fails(
    app_engine: AsyncEngine, project_id: UUID
) -> None:
    async def build(conn: AsyncConnection) -> None:
        cash = await account(conn, project_id, "cash")
        entry_id = await entry(conn, project_id)
        await posting(conn, entry_id, project_id, cash, "debit", 100, "USD")

    await statement_fails(app_engine, build, pg.ForeignKeyViolation)


async def test_posting_to_an_account_of_another_project_fails(
    app_engine: AsyncEngine, project_id: UUID
) -> None:
    async def build(conn: AsyncConnection) -> None:
        foreign = await account(conn, new_uuid(), "cash")
        entry_id = await entry(conn, project_id)
        await posting(conn, entry_id, project_id, foreign, "debit", 100)

    await statement_fails(app_engine, build, pg.ForeignKeyViolation)


async def test_posting_to_an_entry_of_another_project_fails(
    app_engine: AsyncEngine, project_id: UUID
) -> None:
    other = new_uuid()

    async def build(conn: AsyncConnection) -> None:
        cash = await account(conn, other, "cash")
        entry_id = await entry(conn, project_id)
        await posting(conn, entry_id, other, cash, "debit", 100)

    await statement_fails(app_engine, build, pg.ForeignKeyViolation)


async def test_amount_must_be_positive(app_engine: AsyncEngine, project_id: UUID) -> None:
    async def build(conn: AsyncConnection) -> None:
        cash = await account(conn, project_id, "cash")
        await posting(conn, await entry(conn, project_id), project_id, cash, "debit", 0)

    await statement_fails(app_engine, build, pg.CheckViolation)


async def test_no_posting_joins_an_applied_entry(app_engine: AsyncEngine, project_id: UUID) -> None:
    async with app_engine.begin() as conn:
        entry_id = await valid_entry(conn, project_id)

    async def late(conn: AsyncConnection) -> None:
        cash = await account(conn, project_id, "late")
        await posting(conn, entry_id, project_id, cash, "debit", 1)

    assert "already applied" in await statement_fails(app_engine, late, pg.RestrictViolation)


# --- append-only and the only balance writer --------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE ledger_postings SET amount = amount + 1 WHERE project_id = :p",
        "DELETE FROM ledger_postings WHERE project_id = :p",
        "UPDATE ledger_accounts SET posted_dr = posted_dr + 1 WHERE project_id = :p",
        "UPDATE ledger_accounts SET version = 0, pending_cr = 0 WHERE project_id = :p",
        "DELETE FROM ledger_accounts WHERE project_id = :p",
        "DELETE FROM journal_entries WHERE project_id = :p",
        "UPDATE journal_entries SET metadata = '{}' WHERE project_id = :p",
        "UPDATE journal_entries SET applied_status = 'pending' WHERE project_id = :p",
        "TRUNCATE ledger_postings",
        "CREATE TABLE fh_app_probe (id int)",
        "CREATE TEMPORARY TABLE fh_app_probe (id int)",
        "ALTER TABLE ledger_postings DISABLE TRIGGER ALL",
        "SELECT * FROM ledger_postings WHERE project_id = :p FOR UPDATE",
    ],
)
async def test_fh_app_lacks_privileges(app_engine: AsyncEngine, project_id: UUID, sql: str) -> None:
    async with app_engine.begin() as conn:
        await valid_entry(conn, project_id)

    async def build(conn: AsyncConnection) -> None:
        await conn.execute(text(sql), {"p": project_id})

    await statement_fails(app_engine, build, pg.InsufficientPrivilege)


async def test_custom_setting_does_not_unlock_balances(
    app_engine: AsyncEngine, project_id: UUID
) -> None:
    async with app_engine.begin() as conn:
        await valid_entry(conn, project_id)

    async def build(conn: AsyncConnection) -> None:
        await conn.execute(text("SET LOCAL fh.ledger_apply = 'on'"))
        await conn.execute(
            text("UPDATE ledger_accounts SET posted_dr = 0 WHERE project_id = :p"),
            {"p": project_id},
        )

    await statement_fails(app_engine, build, pg.InsufficientPrivilege)


async def test_accounts_start_at_zero(app_engine: AsyncEngine, owner_engine: AsyncEngine) -> None:
    sql = (
        "INSERT INTO ledger_accounts (id, project_id, code, currency, type, posted_cr) "
        "VALUES (:id, :p, 'x', 'BRL', 'asset', 5)"
    )

    async def build(conn: AsyncConnection) -> None:
        await conn.execute(text(sql), {"id": new_uuid(), "p": new_uuid()})

    await statement_fails(app_engine, build, pg.InsufficientPrivilege)
    assert "zero balances" in await statement_fails(owner_engine, build, pg.CheckViolation)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE ledger_postings SET amount = amount + 1 WHERE project_id = :p",
        "DELETE FROM ledger_postings WHERE project_id = :p",
        "TRUNCATE ledger_postings",
        "UPDATE ledger_accounts SET posted_dr = posted_dr + 1 WHERE project_id = :p",
        "UPDATE ledger_accounts SET code = 'renamed' WHERE project_id = :p",
        "DELETE FROM ledger_accounts WHERE project_id = :p",
        "DELETE FROM journal_entries WHERE project_id = :p",
        "UPDATE journal_entries SET recipe = 'other' WHERE project_id = :p",
        "UPDATE journal_entries SET status = 'pending' WHERE project_id = :p",
        "UPDATE journal_entries SET applied_status = 'archived' WHERE project_id = :p",
    ],
)
async def test_triggers_hold_even_for_the_owner(
    app_engine: AsyncEngine, owner_engine: AsyncEngine, project_id: UUID, sql: str
) -> None:
    async with app_engine.begin() as conn:
        await valid_entry(conn, project_id)

    async def build(conn: AsyncConnection) -> None:
        await conn.execute(text(sql), {"p": project_id})

    await statement_fails(owner_engine, build, pg.RestrictViolation)


# --- status transitions -----------------------------------------------------------------------


async def test_posted_entry_cannot_go_back_to_pending(
    app_engine: AsyncEngine, project_id: UUID
) -> None:
    async with app_engine.begin() as conn:
        entry_id = await valid_entry(conn, project_id)

    async def build(conn: AsyncConnection) -> None:
        await conn.execute(
            text("UPDATE journal_entries SET status = 'pending' WHERE id = :id"), {"id": entry_id}
        )

    assert "cannot go from posted" in await statement_fails(app_engine, build, pg.RestrictViolation)


async def test_new_entry_cannot_start_archived(app_engine: AsyncEngine, project_id: UUID) -> None:
    async def build(conn: AsyncConnection) -> None:
        await entry(conn, project_id, "archived")

    await statement_fails(app_engine, build, pg.CheckViolation)


async def test_status_change_without_ledger_apply_fails_at_commit(
    app_engine: AsyncEngine, project_id: UUID
) -> None:
    async with app_engine.begin() as conn:
        cash = await account(conn, project_id, "cash")
        revenue = await account(conn, project_id, "revenue", "income")
        entry_id = await entry(conn, project_id, "pending")
        await posting(conn, entry_id, project_id, cash, "debit", 100)
        await posting(conn, entry_id, project_id, revenue, "credit", 100)
        await apply(conn, entry_id)

    async def build(conn: AsyncConnection) -> None:
        await conn.execute(
            text("UPDATE journal_entries SET status = 'posted' WHERE id = :id"), {"id": entry_id}
        )

    assert "not applied" in await commit_fails(app_engine, build, pg.CheckViolation)

    async with app_engine.begin() as conn:  # the supported path: status + ledger_apply
        await conn.execute(
            text("UPDATE journal_entries SET status = 'posted' WHERE id = :id"), {"id": entry_id}
        )
        assert await apply(conn, entry_id) == 2
    sums = await scalar(
        app_engine,
        "SELECT ARRAY[sum(posted_dr), sum(posted_cr), sum(pending_dr), sum(pending_cr)]::bigint[] "
        "FROM ledger_accounts WHERE project_id = :p",
        p=project_id,
    )
    assert sums == [100, 100, 0, 0]


async def test_ledger_apply_rejects_unknown_entry(app_engine: AsyncEngine) -> None:
    async def build(conn: AsyncConnection) -> None:
        await apply(conn, new_uuid())

    await statement_fails(app_engine, build, pg.NoDataFound)


async def test_lock_function_locks_without_writing(
    app_engine: AsyncEngine, project_id: UUID
) -> None:
    async with app_engine.begin() as conn:
        cash = await account(conn, project_id, "cash")
        rows = (
            await conn.execute(
                text("SELECT id, version FROM ledger_lock_accounts(CAST(:ids AS uuid[]))"),
                {"ids": [cash, new_uuid()]},
            )
        ).all()
    assert [(row.id, row.version) for row in rows] == [(cash, 0)]
