"""Refunds through the API: partial sums, the refundable ceiling, provider outcomes, audit."""

from sqlalchemy.ext.asyncio import AsyncEngine

from financial_hub.modules.payments.infrastructure.fake_provider import (
    FakeBehavior,
    FakeRefundBehavior,
)
from tests.integration.identity.support import Api
from tests.integration.payments.helpers import events_of, intent_uuid, scalar, shop


async def test_partial_refunds_sum_up_to_the_capture_and_no_further(
    api: Api, app_engine: AsyncEngine
) -> None:
    async with shop(api) as s:
        s.fake.script(FakeBehavior.SUCCEED_CAPTURED)
        payment = (await s.create()).json()
        pid = payment["id"]
        first = await s.refund(pid, amount=300, reason="damaged item")
        too_much = await s.refund(pid, amount=800)
        float_amount = await s.refund(pid, amount=1.5)
        zero = await s.refund(pid, amount=0)
        partially = (await s.get(f"/{pid}")).json()
        rest = await s.refund(pid)
        refunded = (await s.get(f"/{pid}")).json()
        again = await s.refund(pid)
        one_more = await s.refund(pid, amount=1)
        audits = await scalar(
            app_engine,
            "SELECT count(*) FROM audit_records WHERE project_id = :p "
            "AND action = 'refund.requested'",
            p=s.project_uuid,
        )
        events = await events_of(app_engine, s.project_uuid, pid)

    assert first.status_code == 201, first.text
    refund = first.json()
    assert refund["id"].startswith("rf_")
    assert (refund["amount"], refund["status"], refund["origin"]) == (300, "done", "hub")
    assert refund["reason"] == "damaged item" and refund["payment_id"] == pid
    assert too_much.status_code == 422
    assert too_much.json()["errors"][0]["code"] == "exceeds_refundable"
    assert float_amount.status_code == zero.status_code == 422
    assert partially["status"] == "partially_refunded"
    assert (partially["refunded_amount"], partially["refundable_amount"]) == (300, 700)
    assert (rest.status_code, rest.json()["amount"]) == (201, 700)
    assert refunded["status"] == "refunded"
    assert (refunded["refunded_amount"], refunded["refundable_amount"]) == (1000, 0)
    assert again.status_code == 409
    assert again.json()["type"].endswith("payment-not-refundable")
    assert one_more.status_code == 422
    assert audits == 2
    requested = [data for kind, data in events if kind == "refund.requested"]
    assert requested == [
        {"refund_id": refund["id"], "payment_id": pid},
        {"refund_id": rest.json()["id"], "payment_id": pid},
    ]
    for _kind, data in events:  # ids and statuses only, never PII
        assert "Maria" not in str(data) and "buyer-1" not in str(data)


async def test_a_pending_payment_cannot_be_refunded(api: Api) -> None:
    async with shop(api) as s:
        pending = (await s.create()).json()
        refused = await s.refund(pending["id"], amount=100)

    assert refused.status_code == 409


async def test_provider_refund_outcomes(api: Api, app_engine: AsyncEngine) -> None:
    async with shop(api) as s:
        s.fake.script(FakeBehavior.SUCCEED_CAPTURED)
        payment = (await s.create()).json()
        pid = payment["id"]
        s.fake.script_refunds(FakeRefundBehavior.REJECT, FakeRefundBehavior.SUBMITTED)
        denied = (await s.refund(pid, amount=100)).json()
        submitted = (await s.refund(pid, amount=200)).json()
        holding = (await s.get(f"/{pid}")).json()
        provider_payment_id = payment["provider_payment_id"]
        assert provider_payment_id is not None
        snapshot = await s.fake.get_payment(provider_payment_id)
        provider_refund = next(
            r for r in snapshot.refunds if r.external_reference == submitted["id"]
        )
        s.fake.complete_refund(provider_payment_id, provider_refund.provider_refund_id)
        status = await s.service().refresh(intent_uuid(pid))
        after = (await s.get(f"/{pid}")).json()

    assert denied["status"] == "denied"
    assert submitted["status"] == "submitted"
    assert holding["refundable_amount"] == 800  # denied releases its amount, submitted holds it
    assert status is not None and status.value == "partially_refunded"
    assert {r["id"]: r["status"] for r in after["refunds"]} == {
        denied["id"]: "denied",
        submitted["id"]: "done",
    }
    assert after["refunded_amount"] == 200
