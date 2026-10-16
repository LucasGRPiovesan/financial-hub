"""The external-call protocol (ADR-0012 s3): fencing tokens, the resolver and provider sync.

Each create runs transaction A (intent + attempt), the provider call outside any transaction,
then transaction B guarded by the attempt token. These tests drive the service directly with
the scriptable fake provider, so the races are deterministic.
"""

import asyncio
from contextlib import aclosing
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine

from financial_hub.modules.customers.application import api as customers_api
from financial_hub.modules.payments.application.registry import ProviderRegistry
from financial_hub.modules.payments.application.service import DUE_DATE_TZ, NewPayment
from financial_hub.modules.payments.domain import (
    CreationStatus,
    PaymentStatus,
    ProviderPaymentSnapshot,
)
from financial_hub.modules.payments.infrastructure import repository
from financial_hub.modules.payments.infrastructure.fake_provider import (
    FakeBehavior,
    FakePaymentProvider,
)
from financial_hub.modules.projects.application import api as projects_api
from financial_hub.shared.ids import IdPrefix, parse_id
from financial_hub.shared.money import Currency, Money
from tests.integration.identity.support import Api, csrf, logged_in
from tests.integration.payments.helpers import Shop, intent_uuid, scalar, shop
from tests.integration.projects.helpers import create_account

LEASE_EXPIRED = timedelta(minutes=6)


def new(s: Shop, **overrides: Any) -> NewPayment:
    due = s.api.clock.now().astimezone(DUE_DATE_TZ).date() + timedelta(days=3)
    values: dict[str, Any] = {
        "amount": 1000,
        "currency": "BRL",
        "method": "pix",
        "due_date": due,
        "customer_id": s.customer_id,
    }
    values.update(overrides)
    return NewPayment(**values)


async def creation(engine: AsyncEngine, intent_id: UUID) -> tuple[str, UUID]:
    status = await scalar(
        engine, "SELECT creation_status FROM payment_intents WHERE id = :id", id=intent_id
    )
    token = await scalar(
        engine, "SELECT attempt_id FROM payment_intents WHERE id = :id", id=intent_id
    )
    return status, token


async def provider_rows(engine: AsyncEngine, intent_id: UUID) -> int:
    return await scalar(
        engine, "SELECT count(*) FROM provider_payments WHERE payment_intent_id = :id", id=intent_id
    )


async def attempt_statuses(engine: AsyncEngine, intent_id: UUID) -> list[str]:
    from sqlalchemy import text

    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT status FROM payment_attempts WHERE payment_intent_id = :id "
                "AND kind = 'create_payment' ORDER BY created_at, id"
            ),
            {"id": intent_id},
        )
        return [row.status for row in rows.all()]


# --- timeouts and the resolver --------------------------------------------------------------------


async def test_timeout_after_commit_is_unknown_then_the_lookup_resolves_it(
    api: Api, app_engine: AsyncEngine
) -> None:
    async with shop(api) as s:
        s.fake.script(FakeBehavior.TIMEOUT_AFTER_COMMIT)
        service = s.service()
        view = await service.create(s.scope(), new(s))
        intent_id = intent_uuid(view.id)
        before = await creation(app_engine, intent_id)
        resolved = await service.resolve_payment(intent_id)
        again = await service.resolve_payment(intent_id)
        after = await service.get(s.project_uuid, view.id)

    assert view.status == "processing"
    assert before[0] == "unknown"
    assert resolved is again is CreationStatus.CREATED
    assert after.status == "requires_payment"
    assert s.fake.calls.count("create_payment") == 1  # never blind-retried
    assert s.fake.payment_count == 1
    assert await provider_rows(app_engine, intent_id) == 1
    assert await attempt_statuses(app_engine, intent_id) == ["succeeded"]


async def test_the_resolver_loop_picks_up_unknown_creations(
    api: Api, app_engine: AsyncEngine
) -> None:
    async with shop(api) as s:
        s.fake.script(FakeBehavior.TIMEOUT_AFTER_COMMIT)
        service = s.service()
        view = await service.create(s.scope(), new(s))
        api.clock.advance(LEASE_EXPIRED)
        looked_at = await service.resolve_pending(limit=1000)
        intent_id = intent_uuid(view.id)

    assert looked_at >= 1
    assert (await creation(app_engine, intent_id))[0] == "created"
    assert s.fake.calls.count("create_payment") >= 1
    assert await provider_rows(app_engine, intent_id) == 1


async def test_a_confirmed_absence_resends_under_a_new_token_and_the_old_one_is_stale(
    api: Api, app_engine: AsyncEngine
) -> None:
    async with shop(api) as s:
        s.fake.script(FakeBehavior.TIMEOUT_BEFORE_COMMIT)
        service = s.service()
        view = await service.create(s.scope(), new(s))
        intent_id = intent_uuid(view.id)
        status, old_token = await creation(app_engine, intent_id)
        resolved = await service.resolve_payment(intent_id)
        _, new_token = await creation(app_engine, intent_id)
        async with s.api.factory() as session, session.begin():
            stale = await repository.record_creation(
                session,
                intent_id=intent_id,
                attempt_id=old_token,
                expected=tuple(CreationStatus),
                status=CreationStatus.REJECTED,
                failure_code="late",
                now=s.api.clock.now(),
            )

    assert status == "unknown"
    assert resolved is CreationStatus.CREATED
    assert new_token != old_token
    assert stale is False  # a stale attempt cannot write
    assert (await creation(app_engine, intent_id))[0] == "created"
    assert await attempt_statuses(app_engine, intent_id) == ["absent", "succeeded"]
    assert s.fake.payment_count == 1


async def test_a_retryable_answer_is_treated_as_unknown(api: Api, app_engine: AsyncEngine) -> None:
    async with shop(api) as s:
        s.fake.script(FakeBehavior.RETRYABLE)
        service = s.service()
        view = await service.create(s.scope(), new(s))
        resolved = await service.resolve_payment(intent_uuid(view.id))

    assert view.status == "processing"
    assert resolved is CreationStatus.CREATED
    assert s.fake.payment_count == 1


async def test_a_fresh_request_is_left_to_its_own_call(api: Api, app_engine: AsyncEngine) -> None:
    async with shop(api) as s:
        release = s.fake.hold()
        service = s.service()
        call = asyncio.create_task(service.create(s.scope(), new(s)))
        await s.fake.held.wait()
        intent_id = await scalar(
            app_engine, "SELECT id FROM payment_intents WHERE project_id = :p", p=s.project_uuid
        )
        untouched = await service.resolve_payment(intent_id)
        release.set()
        view = await call

    assert untouched is CreationStatus.CREATE_REQUESTED
    assert s.fake.calls.count("lookup_by_external_reference") == 0
    assert view.status == "requires_payment"


async def test_a_concurrent_resolver_and_a_late_response_converge_on_one_outcome(
    api: Api, app_engine: AsyncEngine
) -> None:
    async with shop(api) as s:
        s.fake.script(FakeBehavior.SUCCEED_CAPTURED)
        release = s.fake.hold()
        service = s.service()
        call = asyncio.create_task(service.create(s.scope(), new(s)))
        await s.fake.held.wait()
        intent_id = await scalar(
            app_engine, "SELECT id FROM payment_intents WHERE project_id = :p", p=s.project_uuid
        )
        api.clock.advance(LEASE_EXPIRED)  # the in-flight call looks abandoned to the resolver

        async def late_response() -> Any:
            release.set()
            return await call

        resolved, view = await asyncio.gather(service.resolve_payment(intent_id), late_response())

    assert resolved is CreationStatus.CREATED
    assert view.status == "succeeded"
    assert s.fake.payment_count == 1
    assert await provider_rows(app_engine, intent_id) == 1
    assert await attempt_statuses(app_engine, intent_id) == ["succeeded"]
    assert len(s.confirmations.calls) == 1  # the ledger hook runs once, by the winner
    changes = await scalar(
        app_engine,
        "SELECT count(*) FROM events WHERE subject = :s AND type = 'payment.status_changed'",
        s=view.id,
    )
    assert changes == 1


class ForgetfulFake(FakePaymentProvider):
    """A provider whose lookup misses a payment once (eventual consistency)."""

    def __init__(self) -> None:
        super().__init__()
        self.misses = 1

    async def lookup_by_external_reference(
        self, external_reference: str
    ) -> ProviderPaymentSnapshot | None:
        if self.misses:
            self.misses -= 1
            self.calls.append("lookup_by_external_reference")
            return None
        return await super().lookup_by_external_reference(external_reference)


async def test_a_duplicate_external_reference_converges_without_a_second_payment(
    api: Api, app_engine: AsyncEngine
) -> None:
    async with shop(api) as s:
        fake = ForgetfulFake()
        fake.namespace = s.fake.namespace + "f"
        fake.script(FakeBehavior.TIMEOUT_AFTER_COMMIT)
        service = s.service(ProviderRegistry(fake=fake))
        view = await service.create(s.scope(), new(s))
        resolved = await service.resolve_payment(intent_uuid(view.id))

    assert resolved is CreationStatus.CREATED
    assert fake.calls.count("create_payment") == 2  # the re-send hit the provider's duplicate
    assert fake.payment_count == 1
    assert await provider_rows(app_engine, intent_uuid(view.id)) == 1


async def test_an_unavailable_lookup_keeps_the_outcome_unknown(
    api: Api, app_engine: AsyncEngine
) -> None:
    async with shop(api) as s:
        s.fake.script(FakeBehavior.TIMEOUT_AFTER_COMMIT)
        service = s.service()
        view = await service.create(s.scope(), new(s))
        s.fake.lookup_unavailable = True
        still = await service.resolve_payment(intent_uuid(view.id))
        s.fake.lookup_unavailable = False
        resolved = await service.resolve_payment(intent_uuid(view.id))
        disabled = await s.service(ProviderRegistry()).resolve_payment(intent_uuid(view.id))

    assert still is CreationStatus.UNKNOWN
    assert resolved is disabled is CreationStatus.CREATED
    assert s.fake.payment_count == 1


async def test_the_resolver_waits_when_the_provider_is_disabled(
    api: Api, app_engine: AsyncEngine
) -> None:
    async with shop(api) as s:
        s.fake.script(FakeBehavior.TIMEOUT_AFTER_COMMIT)
        view = await s.service().create(s.scope(), new(s))
        waiting = await s.service(ProviderRegistry()).resolve_payment(intent_uuid(view.id))
        resolved = await s.service().resolve_payment(intent_uuid(view.id))
        missing = await s.service().resolve_payment(UUID(int=0))

    assert waiting is CreationStatus.UNKNOWN
    assert resolved is CreationStatus.CREATED
    assert missing is None


# --- provider sync --------------------------------------------------------------------------------


async def test_refresh_moves_capture_settlement_and_records_provider_refunds(
    api: Api, app_engine: AsyncEngine
) -> None:
    async with shop(api) as s:
        service = s.service()
        view = await service.create(s.scope(), new(s))
        intent_id = intent_uuid(view.id)
        pid = view.provider_payment_id
        assert pid is not None
        s.fake.capture(pid, fee=99)
        captured = await service.refresh(intent_id)
        s.fake.settle(pid)
        settled = await service.refresh(intent_id)
        unchanged = await service.refresh(intent_id)
        s.fake.refund_externally(pid, Money(100, Currency.of("BRL")))
        partially = await service.refresh(intent_id)
        after = await service.get(s.project_uuid, view.id)
        no_payment = await service.refresh(UUID(int=0))

    assert captured is PaymentStatus.SUCCEEDED
    assert settled is unchanged is PaymentStatus.SETTLED
    assert partially is PaymentStatus.PARTIALLY_REFUNDED
    assert s.confirmations.calls == [(pid, False)]
    assert (after.captured_amount, after.refunded_amount, after.refundable_amount) == (
        1000,
        100,
        900,
    )
    assert [(r.origin, r.amount, r.status) for r in after.refunds] == [("provider", 100, "done")]
    assert no_payment is None


async def test_a_late_capture_after_a_failure_is_confirmed_as_late(api: Api) -> None:
    async with shop(api) as s:
        service = s.service()
        view = await service.create(s.scope(), new(s))
        pid = view.provider_payment_id
        assert pid is not None
        s.fake.fail(pid)
        failed = await service.refresh(intent_uuid(view.id))
        s.fake.capture(pid)
        late = await service.refresh(intent_uuid(view.id))

    assert failed is PaymentStatus.FAILED
    assert late is PaymentStatus.SUCCEEDED
    assert s.confirmations.calls == [(pid, True)]


# --- refund resolver ------------------------------------------------------------------------------


async def test_unknown_refunds_are_resolved_by_lookup_or_resent(
    api: Api, app_engine: AsyncEngine
) -> None:
    from financial_hub.modules.payments.infrastructure.fake_provider import FakeRefundBehavior

    async with shop(api) as s:
        s.fake.script(FakeBehavior.SUCCEED_CAPTURED)
        payment = (await s.create()).json()
        s.fake.script_refunds(FakeRefundBehavior.TIMEOUT_AFTER_COMMIT, FakeRefundBehavior.RETRYABLE)
        first = (await s.refund(payment["id"], amount=100)).json()
        second = (await s.refund(payment["id"], amount=200)).json()
        service = s.service()
        api.clock.advance(LEASE_EXPIRED)
        found = await service.resolve_refund(parse_id(first["id"], IdPrefix.REFUND))
        resent = await service.resolve_refund(parse_id(second["id"], IdPrefix.REFUND))
        settled = await service.resolve_refund(parse_id(first["id"], IdPrefix.REFUND))
        missing = await service.resolve_refund(UUID(int=0))
        after = (await s.get(f"/{payment['id']}")).json()

    assert (first["status"], second["status"]) == ("unknown", "unknown")
    assert found is resent is settled
    assert found is not None and found.value == "done"
    assert missing is None
    assert s.fake.calls.count("refund") == 3  # first once; second: retryable, then re-sent once
    assert after["refunded_amount"] == 300
    assert after["status"] == "partially_refunded"


# --- cross-module read helpers --------------------------------------------------------------------


async def test_the_read_helpers_of_projects_and_customers(api: Api) -> None:
    async with shop(api) as s, shop(api) as other:
        client, token = await logged_in(api, s.owner)
        async with aclosing(client):
            account = await create_account(client, token, s.project_id)
            anonymized = await client.put(
                f"{s.base.removesuffix('/payments')}/customers/gone-1",
                json={"name": "Gone", "kind": "individual"},
                headers=csrf(token),
            )
            assert anonymized.status_code == 201, anonymized.text
            gone_id = anonymized.json()["id"]
            erased = await client.post(
                f"{s.base.removesuffix('/payments')}/customers/{gone_id}/anonymize",
                headers=csrf(token),
            )
            assert erased.status_code == 200, erased.text
        account_id = parse_id(account["id"], IdPrefix.PROVIDER_ACCOUNT)
        async with s.api.factory() as session, session.begin():
            ref = await projects_api.provider_account_ref(session, s.project_uuid, account_id)
            foreign = await projects_api.provider_account_ref(
                session, other.project_uuid, account_id
            )
            active = await projects_api.active_provider_accounts(session, s.project_uuid)
            none = await projects_api.active_provider_accounts(session, other.project_uuid)

            def resolve(project: UUID, **ref: str | None) -> Any:
                values: dict[str, str | None] = {"customer_id": None, "external_ref": None}
                values.update(ref)
                return customers_api.resolve_customer_identity(
                    session, project_id=project, **values
                )

            by_id = await resolve(s.project_uuid, customer_id=s.customer_id)
            by_ref = await resolve(s.project_uuid, external_ref="buyer-1")
            both = await resolve(s.project_uuid, customer_id=s.customer_id, external_ref="buyer-1")
            mismatch = await resolve(
                s.project_uuid, customer_id=other.customer_id, external_ref="buyer-1"
            )
            elsewhere = await resolve(other.project_uuid, customer_id=s.customer_id)
            gone = await resolve(s.project_uuid, customer_id=gone_id)
            malformed = await resolve(s.project_uuid, customer_id="cus_nope")

    assert ref is not None and (ref.id, ref.provider, ref.active) == (account_id, "asaas", True)
    assert not hasattr(ref, "ciphertext") and "secret" not in repr(ref).lower()
    assert foreign is None
    assert [a.id for a in active] == [account_id]
    assert list(none) == []
    assert by_id is not None and by_id == by_ref == both
    assert mismatch is None and elsewhere is None and gone is None and malformed is None
