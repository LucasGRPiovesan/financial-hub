"""The reconciliation HTTP surface: runs, break transitions, four-eyes R12 adjustments, RBAC."""

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from financial_hub.modules.identity.application.api import Role
from financial_hub.shared.money import BRL, Money
from tests.integration.identity.support import Api, Operator
from tests.integration.ledger.support import Ledger
from tests.integration.payments.helpers import events_of
from tests.integration.projects.helpers import bearer
from tests.integration.reconciliation.helpers import Desk, desk, member


class Console:
    """One operator's console session on the desk's project."""

    def __init__(self, d: Desk, client: httpx.AsyncClient, headers: dict[str, str]) -> None:
        self.d, self.client, self.headers = d, client, headers

    async def get(self, path: str, **params: Any) -> httpx.Response:
        return await self.client.get(f"{self.d.base}{path}", params=params)

    async def post(
        self, path: str, body: dict[str, Any], *, idempotent: bool = False
    ) -> httpx.Response:
        headers = dict(self.headers)
        if idempotent:
            headers["Idempotency-Key"] = secrets.token_hex(8)
        return await self.client.post(f"{self.d.base}{path}", json=body, headers=headers)

    async def run(self, kind: str, **body: Any) -> httpx.Response:
        return await self.post("/runs", {"kind": kind, **body}, idempotent=True)


@asynccontextmanager
async def console(d: Desk, operator: Operator) -> AsyncIterator[Console]:
    client, headers = await d.session(operator)
    try:
        yield Console(d, client, headers)
    finally:
        await client.aclose()


@asynccontextmanager
async def drifted(api: Api) -> AsyncIterator[Desk]:
    """A settled payment (net 901 booked) and a provider reporting 1001, on yesterday.

    Consoles log in afterwards: moving the clock a day expires earlier sessions.
    """
    async with desk(api) as d:
        await d.settled()
        d.shop.fake.balance_override = Money(1001, BRL)
        d.next_day()
        yield d


async def balance_break(d: Desk, owner: Console) -> dict[str, Any]:
    """Run the balance check of yesterday: one balance_drift (see :func:`drifted`)."""
    run = await owner.run("balance")
    assert run.status_code == 200, run.text
    listed = await owner.get("/breaks", kind="balance_drift", run_id=run.json()["id"])
    [found] = listed.json()["data"]
    return found


async def r12_entries(engine: AsyncEngine, d: Desk) -> int:
    async with engine.connect() as conn:
        found = await conn.execute(
            text("SELECT count(*) FROM journal_entries WHERE project_id = :p AND recipe = 'R12'"),
            {"p": d.project_uuid},
        )
        return found.scalar_one()


def problem(response: httpx.Response, status: int, slug: str) -> None:
    assert response.status_code == status, response.text
    assert response.json()["type"].endswith(slug), response.text


async def test_an_approved_adjustment_posts_r12_and_resolves_the_break(
    api: Api, app_engine: AsyncEngine
) -> None:
    async with (
        drifted(api) as d,
        console(d, d.shop.owner) as owner,
        console(d, d.finance) as finance,
    ):
        found = await balance_break(d, owner)
        proposed = await owner.post(
            f"/breaks/{found['id']}/adjustments",
            {"amount_minor": 100, "reason": "provider credited a bonus"},
            idempotent=True,
        )
        assert proposed.status_code == 201, proposed.text
        adjustment = proposed.json()
        before = await r12_entries(app_engine, d)
        self_approved = await owner.post(
            f"/adjustments/{adjustment['id']}/approve", {"payload_hash": adjustment["payload_hash"]}
        )
        self_rejected = await owner.post(
            f"/adjustments/{adjustment['id']}/reject", {"reason": "changed my mind"}
        )
        tampered = await finance.post(
            f"/adjustments/{adjustment['id']}/approve", {"payload_hash": "0" * 64}
        )
        approved = await finance.post(
            f"/adjustments/{adjustment['id']}/approve", {"payload_hash": adjustment["payload_hash"]}
        )
        again = await finance.post(
            f"/adjustments/{adjustment['id']}/approve", {"payload_hash": adjustment["payload_hash"]}
        )
        after = await r12_entries(app_engine, d)
        closed = await owner.get(f"/breaks/{found['id']}")
        await Ledger(api.factory).assert_consistent(d.project_uuid)
        yesterday = api.clock.now().date() - timedelta(days=2)
        rerun = await owner.run(
            "balance", window_start=yesterday.isoformat(), window_end=yesterday.isoformat()
        )
        resolved = await events_of(app_engine, d.project_uuid, found["id"])

    assert adjustment["status"] == "proposed"
    assert adjustment["amount_minor"] == 100
    assert adjustment["proposed_by"] == d.shop.owner.id
    problem(self_approved, 403, "four-eyes-required")
    problem(self_rejected, 403, "four-eyes-required")
    problem(tampered, 409, "adjustment-payload-mismatch")
    assert before == 0
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    assert approved.json()["decided_by"] == d.finance.id
    assert approved.json()["journal_entry_id"].startswith("je_")
    problem(again, 409, "adjustment-decided")
    assert after == 1
    assert closed.json()["state"] == "resolved"
    assert closed.json()["resolved_by"] == d.finance.id
    assert rerun.json()["breaks_opened"] == 0  # the books now agree with the provider
    assert rerun.json()["summary"]["booked_minor"] == 1001
    assert [type_ for type_, _ in resolved][-1] == "reconciliation.break_resolved"


async def test_a_rejected_adjustment_posts_nothing(api: Api, app_engine: AsyncEngine) -> None:
    async with (
        drifted(api) as d,
        console(d, d.shop.owner) as owner,
        console(d, d.finance) as finance,
    ):
        found = await balance_break(d, owner)
        proposed = await finance.post(
            f"/breaks/{found['id']}/adjustments",
            {"amount_minor": 100, "reason": "provider credited a bonus"},
            idempotent=True,
        )
        second = await owner.post(
            f"/breaks/{found['id']}/adjustments",
            {"amount_minor": 50, "reason": "a competing proposal"},
            idempotent=True,
        )
        adjustment = proposed.json()
        blank = await owner.post(f"/adjustments/{adjustment['id']}/reject", {"reason": "  "})
        rejected = await owner.post(
            f"/adjustments/{adjustment['id']}/reject", {"reason": "the bonus was reversed"}
        )
        late = await owner.post(
            f"/adjustments/{adjustment['id']}/approve", {"payload_hash": adjustment["payload_hash"]}
        )
        still = await owner.get(f"/breaks/{found['id']}")
        listed = await owner.get("/adjustments", break_id=found["id"], status="rejected")
        entries = await r12_entries(app_engine, d)
        await Ledger(api.factory).assert_consistent(d.project_uuid)

    assert proposed.status_code == 201, proposed.text
    problem(second, 409, "adjustment-pending")
    assert blank.status_code == 422, blank.text
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["journal_entry_id"] is None
    problem(late, 409, "adjustment-decided")
    assert still.json()["state"] == "open"
    assert [item["id"] for item in listed.json()["data"]] == [adjustment["id"]]
    assert entries == 0


async def test_break_transitions_need_reasons_and_never_reopen(api: Api) -> None:
    async with drifted(api) as d, console(d, d.shop.owner) as owner:
        found = await balance_break(d, owner)
        path = f"/breaks/{found['id']}/transition"
        silent = await owner.post(path, {"state": "investigating"})
        investigating = await owner.post(path, {"state": "investigating", "reason": "asked Asaas"})
        reopen = await owner.post(path, {"state": "open", "reason": "again"})
        unexplained = await owner.post(path, {"state": "accepted"})
        accepted = await owner.post(path, {"state": "accepted", "reason": "shared sandbox noise"})
        after = await owner.post(path, {"state": "resolved"})
        proposal = await owner.post(
            f"/breaks/{found['id']}/adjustments",
            {"amount_minor": 100, "reason": "too late"},
            idempotent=True,
        )

    assert silent.status_code == 422, silent.text
    assert investigating.status_code == 200, investigating.text
    assert investigating.json()["state"] == "investigating"
    assert reopen.status_code == 422, reopen.text
    assert unexplained.status_code == 422, unexplained.text
    assert accepted.json()["state"] == "accepted"
    assert accepted.json()["resolution"] == "shared sandbox noise"
    assert [entry["to"] for entry in accepted.json()["history"]] == [
        "open",
        "investigating",
        "accepted",
    ]
    problem(after, 409, "illegal-break-transition")
    problem(proposal, 409, "break-closed")


async def test_roles_split_running_from_adjusting(api: Api) -> None:
    async with drifted(api) as d, console(d, d.shop.owner) as owner:
        found = await balance_break(d, owner)
        admin, viewer = await member(d, Role.ADMIN), await member(d, Role.VIEWER)
        async with console(d, admin) as as_admin, console(d, viewer) as as_viewer:
            viewer_reads = await as_viewer.get("/breaks")
            viewer_runs = await as_viewer.run("internal")
            viewer_moves = await as_viewer.post(
                f"/breaks/{found['id']}/transition", {"state": "investigating", "reason": "x"}
            )
            admin_runs = await as_admin.run("internal")
            admin_moves = await as_admin.post(
                f"/breaks/{found['id']}/transition", {"state": "investigating", "reason": "x"}
            )
            admin_proposes = await as_admin.post(
                f"/breaks/{found['id']}/adjustments",
                {"amount_minor": 100, "reason": "x"},
                idempotent=True,
            )
        async with api.client() as client:
            key_reads = await client.get(f"{d.base}/breaks", headers=bearer(d.shop.key))
            key_runs = await client.post(
                f"{d.base}/runs",
                json={"kind": "internal"},
                headers={**bearer(d.shop.key), "Idempotency-Key": secrets.token_hex(8)},
            )

    assert viewer_reads.status_code == 200, viewer_reads.text
    assert [item["id"] for item in viewer_reads.json()["data"]] == [found["id"]]
    assert viewer_runs.status_code == 403
    assert viewer_moves.status_code == 403
    assert admin_runs.status_code == 200, admin_runs.text
    assert admin_moves.status_code == 200, admin_moves.text
    assert admin_proposes.status_code == 403
    assert key_reads.status_code == 403
    assert key_runs.status_code == 403


async def test_runs_validate_their_window(api: Api) -> None:
    async with desk(api) as d, console(d, d.shop.owner) as owner:
        today = api.clock.now().date()
        future = await owner.run(
            "internal", window_start=today.isoformat(), window_end=today.isoformat()
        )
        half = await owner.run("internal", window_start=today.isoformat())
        wrong = await owner.run("everything")
        missing = await owner.get("/runs/rec_00000000000000000000000000")
        listed = await owner.get("/runs", kind="internal")

    assert future.status_code == 422, future.text
    assert half.status_code == 422, half.text
    assert wrong.status_code == 422, wrong.text
    assert missing.status_code == 404, missing.text
    assert listed.json()["data"] == []
