"""Inbound Asaas webhooks end to end over a scripted sandbox (W4.4, docs/INTEGRATIONS.md s2).

Real Postgres, real provider-accounts API and credentials vault, the real route; only the network
is replaced (the ``SandboxAsaas`` transport). Proves the token check (one 401 for every failure),
the inbox dedup (a redelivery is one row and one transition), that amounts always come from the
refetch (a forged body moves nothing), that order does not matter, and the sweeper.
"""

from contextlib import aclosing
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from financial_hub.modules.events.application import Event, HandlerRegistry
from financial_hub.modules.payments.application.config import PaymentsSettings
from financial_hub.modules.payments.application.registry import (
    ProviderRegistry,
    customers_contact_loader,
    projects_credentials_loader,
)
from financial_hub.modules.payments.application.service import DEFAULT_LEASE, PaymentService
from financial_hub.modules.payments.application.webhooks import (
    INTERNAL_PROJECT_ID,
    SWEEPER_HANDLER,
    SWEEPER_SCHEDULE,
    WEBHOOK_HANDLER,
    WebhookProcessor,
    ensure_sweeper_schedule,
    register_payment_handlers,
)
from financial_hub.modules.payments.domain import CreationStatus, WebhookStatus
from financial_hub.platform.db import UnitOfWork
from financial_hub.shared.ids import IdPrefix, parse_id
from tests.integration.identity.support import Api, logged_in
from tests.integration.payments.helpers import Shop, events_of, scalar, shop
from tests.integration.payments.test_asaas_api import (
    FAKE_KEY,
    SandboxAsaas,
    creation_status,
    set_active,
    wire,
)
from tests.integration.payments.webhooks import notification, post_webhook
from tests.integration.projects.helpers import create_account

TOKEN = "whk-test-token-0123456789abcdefghijklmnop"
REJECTED = {
    "status": 401,
    "title": "Authentication required",
    "detail": "Invalid webhook credentials.",
}


class FlakyAsaas(SandboxAsaas):
    """The sandbox, plus an outage switch for ``GET /payments/{id}`` (the refetch)."""

    def __init__(self) -> None:
        super().__init__()
        self.refetch_down = False
        self.refetches = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.startswith("/v3/payments/"):
            self.refetches += 1
            if self.refetch_down:
                return httpx.Response(503, json={"errors": [{"code": "unavailable"}]})
        return super().__call__(request)


async def hooked_account(s: Shop, *, token: str = TOKEN, name: str = "main") -> str:
    client, csrf_token = await logged_in(s.api, s.owner)
    async with aclosing(client):
        account = await create_account(
            client,
            csrf_token,
            s.project_id,
            name=name,
            credentials={"api_key": FAKE_KEY, "webhook_token": token},
        )
    return account["id"]


def processor_for(s: Shop, registry: ProviderRegistry, **settings: Any) -> WebhookProcessor:
    keyring = s.api.app.state.keyring
    service = PaymentService(
        s.api.factory,
        clock=s.api.clock,
        registry=registry,
        credentials=projects_credentials_loader(keyring),
        contacts=customers_contact_loader(keyring),
    )
    return WebhookProcessor(
        s.api.factory, service, clock=s.api.clock, settings=PaymentsSettings(**settings)
    )


async def inbox(engine: AsyncEngine, account_id: str) -> list[Any]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT * FROM provider_webhook_events WHERE provider_account_id = :a "
                "ORDER BY received_at, id"
            ),
            {"a": parse_id(account_id, IdPrefix.PROVIDER_ACCOUNT)},
        )
        return list(rows.all())


async def status_changes(engine: AsyncEngine, s: Shop, payment_id: str) -> list[str]:
    return [
        data["status"]
        for kind, data in await events_of(engine, s.project_uuid, payment_id)
        if kind == "payment.status_changed"
    ]


async def charge(s: Shop, asaas: SandboxAsaas, account_id: str) -> tuple[str, dict[str, Any]]:
    """A payment created at the sandbox: our ``pi_`` id and the remote payment."""
    view = (await s.create(provider_account_id=account_id, amount=1000)).json()
    [remote] = [p for p in asaas.payments.values() if p["externalReference"] == view["id"]]
    return view["id"], remote


# --- receiving: authentication ------------------------------------------------------------------


async def test_a_notification_is_stored_enqueued_and_acknowledged(
    api: Api, app_engine: AsyncEngine
) -> None:
    asaas = SandboxAsaas()
    async with shop(api) as s:
        wire(api, asaas)
        account_id = await hooked_account(s)
        payment_id, remote = await charge(s, asaas, account_id)

        response = await post_webhook(api, account_id, TOKEN, notification(remote["id"]))
        [row] = await inbox(app_engine, account_id)
        enqueued = await events_of(app_engine, INTERNAL_PROJECT_ID, f"webhook:{row.id}")
        in_project = await events_of(app_engine, s.project_uuid, f"webhook:{row.id}")

    assert response.status_code == 200, response.text
    assert response.json() == {"received": True}
    assert row.status == "received" and row.attempts == 0 and row.event_type == "PAYMENT_RECEIVED"
    assert row.project_id == s.project_uuid
    assert remote["id"] in row.body  # the raw body, as received
    assert enqueued == [("payments.webhook_received", {"webhook_id": str(row.id)})]
    assert in_project == []  # internal: no project stream or customer endpoint sees it
    assert payment_id  # the payment is untouched until the worker processes the row


async def test_every_authentication_failure_is_the_same_401_and_stores_nothing(
    api: Api, app_engine: AsyncEngine
) -> None:
    asaas = SandboxAsaas()
    async with shop(api) as s, shop(api) as other:
        wire(api, asaas)
        account_id = await hooked_account(s)
        short = await hooked_account(s, token="short-token", name="short")
        plain_client, csrf_token = await logged_in(api, s.owner)
        async with aclosing(plain_client):
            no_token = (
                await create_account(
                    plain_client,
                    csrf_token,
                    s.project_id,
                    name="no-token",
                    credentials={"api_key": FAKE_KEY},
                )
            )["id"]
        disabled = await hooked_account(other)
        await set_active(other, disabled, active=False)
        body = notification("pay_000001")

        answers = {
            "wrong token": await post_webhook(api, account_id, TOKEN[:-1] + "x", body),
            "missing header": await post_webhook(api, account_id, None, body),
            "empty header": await post_webhook(api, account_id, "", body),
            "short configured token": await post_webhook(api, short, "short-token", body),
            "no configured token": await post_webhook(api, no_token, TOKEN, body),
            "disabled account": await post_webhook(api, disabled, TOKEN, body),
            "unknown account": await post_webhook(api, "pa_" + "0" * 22, TOKEN, body),
            "not an account id": await post_webhook(api, "cus_123", TOKEN, body),
            "garbage body, wrong token": await post_webhook(api, account_id, "x", b"{"),
        }
        stored = [len(await inbox(app_engine, a)) for a in (account_id, short, no_token, disabled)]

    for case, response in answers.items():
        assert response.status_code == 401, (case, response.text)
        problem = response.json()
        # the same answer everywhere; ``instance`` only echoes the path the caller sent
        assert {k: problem[k] for k in REJECTED} == REJECTED, case
        assert set(problem) <= {*REJECTED, "type", "instance", "errors", "correlation_id"}, case
        assert TOKEN not in response.text and "short-token" not in response.text
    assert stored == [0, 0, 0, 0]


async def test_an_authenticated_body_that_is_not_a_notification_is_422(
    api: Api, app_engine: AsyncEngine
) -> None:
    asaas = SandboxAsaas()
    async with shop(api) as s:
        wire(api, asaas)
        account_id = await hooked_account(s)

        answers = [
            await post_webhook(api, account_id, TOKEN, body)
            for body in (b"{", b"[]", b'{"event": "PAYMENT_RECEIVED"}', b"\xff\xfe")
        ]
        stored = await inbox(app_engine, account_id)

    assert [r.status_code for r in answers] == [422, 422, 422, 422]
    assert stored == []


async def test_a_body_over_the_limit_is_refused_before_it_is_read(
    api: Api, app_engine: AsyncEngine
) -> None:
    asaas = SandboxAsaas()
    async with shop(api) as s:
        wire(api, asaas)
        account_id = await hooked_account(s)
        big = notification("pay_000001", padding="x" * (300 * 1024))

        refused = await post_webhook(api, account_id, TOKEN, big)
        stored = await inbox(app_engine, account_id)

    assert refused.status_code == 413, refused.text
    assert stored == []


# --- dedup and processing -------------------------------------------------------------------------


async def test_a_duplicate_delivery_is_one_row_and_one_transition(
    api: Api, app_engine: AsyncEngine
) -> None:
    asaas = SandboxAsaas()
    async with shop(api) as s:
        registry = wire(api, asaas)
        account_id = await hooked_account(s)
        payment_id, remote = await charge(s, asaas, account_id)
        remote["status"] = "RECEIVED"
        body = notification(remote["id"], payment_id)
        processor = processor_for(s, registry)

        first = await post_webhook(api, account_id, TOKEN, body)
        again = await post_webhook(api, account_id, TOKEN, body)
        [row] = await inbox(app_engine, account_id)
        processed = await processor.process(row.id)
        reprocessed = await processor.process(row.id)  # a redelivered queue message
        late = await post_webhook(api, account_id, TOKEN, body)
        rows = await inbox(app_engine, account_id)
        enqueued = await scalar(
            app_engine,
            "SELECT count(*) FROM events WHERE project_id = :nil AND subject = :subject",
            nil=INTERNAL_PROJECT_ID,
            subject=f"webhook:{row.id}",
        )
        changes = await status_changes(app_engine, s, payment_id)
        captures = await scalar(
            app_engine,
            "SELECT count(*) FROM journal_entries WHERE project_id = :p AND recipe = 'R4'",
            p=s.project_uuid,
        )
        view = (await s.get(f"/{payment_id}")).json()

    assert [first.status_code, again.status_code, late.status_code] == [200, 200, 200]
    assert len(rows) == 1 and enqueued == 1
    assert processed is reprocessed is WebhookStatus.PROCESSED
    assert rows[0].status == "processed" and rows[0].outcome == "refreshed:settled"
    assert rows[0].processed_at is not None
    assert changes == ["requires_payment", "settled"]  # created, then exactly one transition
    assert captures == 1
    assert view["status"] == "settled"


async def test_a_forged_amount_is_ignored_and_the_ledger_follows_the_refetch(
    api: Api, app_engine: AsyncEngine
) -> None:
    asaas = SandboxAsaas()
    async with shop(api) as s:
        registry = wire(api, asaas)
        account_id = await hooked_account(s)
        payment_id, remote = await charge(s, asaas, account_id)
        remote["status"] = "RECEIVED"  # what Asaas really holds: R$ 10.00 received
        forged = notification(
            remote["id"], payment_id, value=999999.99, netValue=999999.99, status="RECEIVED"
        )

        await post_webhook(api, account_id, TOKEN, forged)
        [row] = await inbox(app_engine, account_id)
        await processor_for(s, registry).process(row.id)
        view = (await s.get(f"/{payment_id}")).json()
        largest = await scalar(
            app_engine,
            "SELECT max(amount) FROM ledger_postings WHERE project_id = :p",
            p=s.project_uuid,
        )

    assert view["status"] == "settled"
    assert view["amount"] == 1000 and view["captured_amount"] == 1000
    assert largest == 1000  # nothing in the ledger came from the body


async def test_a_forged_status_cannot_settle_an_unpaid_payment(
    api: Api, app_engine: AsyncEngine
) -> None:
    asaas = SandboxAsaas()
    async with shop(api) as s:
        registry = wire(api, asaas)
        account_id = await hooked_account(s)
        payment_id, remote = await charge(s, asaas, account_id)  # still PENDING at Asaas

        await post_webhook(api, account_id, TOKEN, notification(remote["id"], payment_id))
        [row] = await inbox(app_engine, account_id)
        await processor_for(s, registry).process(row.id)
        view = (await s.get(f"/{payment_id}")).json()
        [processed] = await inbox(app_engine, account_id)

    assert view["status"] == "requires_payment"
    assert processed.outcome == "refreshed:requires_payment"


async def test_received_then_created_out_of_order_ends_settled(
    api: Api, app_engine: AsyncEngine
) -> None:
    asaas = SandboxAsaas()
    async with shop(api) as s:
        registry = wire(api, asaas)
        account_id = await hooked_account(s)
        payment_id, remote = await charge(s, asaas, account_id)
        remote["status"] = "RECEIVED"
        processor = processor_for(s, registry)

        received = notification(remote["id"], payment_id, event="PAYMENT_RECEIVED")
        created = notification(remote["id"], payment_id, event="PAYMENT_CREATED", status="PENDING")
        await post_webhook(api, account_id, TOKEN, received)
        await post_webhook(api, account_id, TOKEN, created)
        rows = await inbox(app_engine, account_id)
        for row in rows:
            await processor.process(row.id)
        after = await inbox(app_engine, account_id)
        changes = await status_changes(app_engine, s, payment_id)
        view = (await s.get(f"/{payment_id}")).json()

    assert [r.event_type for r in rows] == ["PAYMENT_RECEIVED", "PAYMENT_CREATED"]
    assert [r.outcome for r in after] == ["refreshed:settled", "refreshed:settled"]
    assert changes == ["requires_payment", "settled"]  # the late CREATED moved nothing
    assert view["status"] == "settled"


async def test_unknown_ignored_and_foreign_notifications_are_kept_and_acknowledged(
    api: Api, app_engine: AsyncEngine
) -> None:
    asaas = SandboxAsaas()
    async with shop(api) as s:
        registry = wire(api, asaas)
        account_id = await hooked_account(s)
        second = await hooked_account(s, name="second")
        payment_id, remote = await charge(s, asaas, account_id)
        processor = processor_for(s, registry)
        bodies = [
            notification(remote["id"], payment_id, event="SOMETHING_NEW_FROM_ASAAS"),
            notification(remote["id"], payment_id, event="PAYMENT_BANK_SLIP_VIEWED"),
            notification("pay_999999", None),  # a payment the hub never created
            notification("pay_999998", "pi_" + "0" * 22),  # a reference that names nothing
        ]

        answers = [await post_webhook(api, account_id, TOKEN, body) for body in bodies]
        # the second account's token, naming the first account's payment
        foreign = await post_webhook(api, second, TOKEN, notification(remote["id"], payment_id))
        rows = await inbox(app_engine, account_id) + await inbox(app_engine, second)
        for row in rows:
            await processor.process(row.id)
        after = await inbox(app_engine, account_id) + await inbox(app_engine, second)
        changes = await status_changes(app_engine, s, payment_id)

    assert [r.status_code for r in [*answers, foreign]] == [200] * 5
    assert [(r.status, r.outcome) for r in after] == [
        ("parked", "unsupported_event_type"),
        ("ignored", "ignored_event_type"),
        ("ignored", "unknown_payment"),
        ("ignored", "unknown_payment"),
        ("parked", "binding_mismatch"),
    ]
    assert changes == ["requires_payment"]


async def test_a_notification_that_beats_an_unknown_creation_converges_it_first(
    api: Api, app_engine: AsyncEngine
) -> None:
    asaas = SandboxAsaas()
    asaas.fail_next_post = httpx.ReadTimeout("response lost")
    asaas.store_before_failing = True  # created at Asaas; our answer never arrived
    async with shop(api) as s:
        registry = wire(api, asaas)
        account_id = await hooked_account(s)
        view = (await s.create(provider_account_id=account_id)).json()
        [remote] = asaas.payments.values()
        remote["status"] = "RECEIVED"
        before = await creation_status(app_engine, view["id"])

        await post_webhook(api, account_id, TOKEN, notification(remote["id"], view["id"]))
        [row] = await inbox(app_engine, account_id)
        status = await processor_for(s, registry).process(row.id)
        after = await creation_status(app_engine, view["id"])
        [processed] = await inbox(app_engine, account_id)

    assert before == "unknown"
    assert status is WebhookStatus.PROCESSED
    assert after == "created" and processed.outcome == "refreshed:settled"
    assert asaas.post_count == 1 and len(asaas.payments) == 1  # converged, never re-sent


# --- failures and the sweeper ---------------------------------------------------------------------


async def test_a_failed_refetch_is_retried_by_the_sweeper(
    api: Api, app_engine: AsyncEngine
) -> None:
    asaas = FlakyAsaas()
    async with shop(api) as s:
        registry = wire(api, asaas)
        account_id = await hooked_account(s)
        payment_id, remote = await charge(s, asaas, account_id)
        remote["status"] = "RECEIVED"
        processor = processor_for(s, registry, webhook_retry_after_seconds=60)

        await post_webhook(api, account_id, TOKEN, notification(remote["id"], payment_id))
        [row] = await inbox(app_engine, account_id)
        asaas.refetch_down = True
        failed = await processor.process(row.id)
        [stored] = await inbox(app_engine, account_id)
        asaas.refetch_down = False
        await processor.sweep()  # too recent: left for later
        [waiting] = await inbox(app_engine, account_id)
        api.clock.advance(timedelta(seconds=61))
        await processor.sweep()
        [done] = await inbox(app_engine, account_id)
        view = (await s.get(f"/{payment_id}")).json()

    assert failed is WebhookStatus.FAILED
    assert stored.status == "failed" and stored.attempts == 1
    assert stored.last_error == "refetch_failed" and stored.processed_at is None
    assert waiting.status == "failed"
    assert done.status == "processed" and done.outcome == "refreshed:settled"
    assert view["status"] == "settled"


async def test_a_row_out_of_attempts_is_left_for_a_person(
    api: Api, app_engine: AsyncEngine
) -> None:
    asaas = FlakyAsaas()
    asaas.refetch_down = True
    async with shop(api) as s:
        registry = wire(api, asaas)
        account_id = await hooked_account(s)
        payment_id, remote = await charge(s, asaas, account_id)
        processor = processor_for(
            s, registry, webhook_max_attempts=2, webhook_retry_after_seconds=1
        )

        await post_webhook(api, account_id, TOKEN, notification(remote["id"], payment_id))
        [row] = await inbox(app_engine, account_id)
        await processor.process(row.id)
        for _ in range(3):
            api.clock.advance(timedelta(seconds=2))
            await processor.sweep()
        [stored] = await inbox(app_engine, account_id)

    assert stored.status == "failed" and stored.attempts == 2


async def test_the_sweeper_resolves_an_unknown_creation(api: Api, app_engine: AsyncEngine) -> None:
    asaas = SandboxAsaas()
    asaas.fail_next_post = httpx.ReadTimeout("response lost")
    asaas.store_before_failing = True
    async with shop(api) as s:
        registry = wire(api, asaas)
        account_id = await hooked_account(s)
        view = (await s.create(provider_account_id=account_id)).json()
        before = await creation_status(app_engine, view["id"])

        api.clock.advance(DEFAULT_LEASE + timedelta(seconds=1))
        await processor_for(s, registry, sweeper_batch_size=1000).sweep()
        after = await creation_status(app_engine, view["id"])
        mine = [p for p in asaas.payments.values() if p["externalReference"] == view["id"]]

    assert before == "unknown"
    assert after == CreationStatus.CREATED.value
    assert len(mine) == 1  # found by the lookup, not created twice


# --- worker wiring --------------------------------------------------------------------------------


class _Uow:
    def __init__(self) -> None:
        self.after_commit: list[Any] = []


async def test_the_handlers_only_queue_after_commit_work(api: Api, app_engine: AsyncEngine) -> None:
    asaas = SandboxAsaas()
    async with shop(api) as s:
        registry = wire(api, asaas)
        account_id = await hooked_account(s)
        payment_id, remote = await charge(s, asaas, account_id)
        remote["status"] = "RECEIVED"
        processor = processor_for(s, registry)
        handlers = HandlerRegistry()
        register_payment_handlers(handlers, processor)

        await post_webhook(api, account_id, TOKEN, notification(remote["id"], payment_id))
        [row] = await inbox(app_engine, account_id)
        [(_, data)] = await events_of(app_engine, INTERNAL_PROJECT_ID, f"webhook:{row.id}")
        on_webhook, on_sweep = handlers.get(WEBHOOK_HANDLER), handlers.get(SWEEPER_HANDLER)
        assert on_webhook is not None and on_sweep is not None
        uow = _Uow()
        await on_webhook(cast("Event", SimpleNamespace(data=data)), cast("UnitOfWork", uow))
        untouched = (await s.get(f"/{payment_id}")).json()["status"]
        for hook in uow.after_commit:
            await hook()
        settled = (await s.get(f"/{payment_id}")).json()["status"]
        sweep_uow = _Uow()
        due = {"schedule": "payments.sweeper", "due_at": api.clock.now().isoformat()}
        await on_sweep(cast("Event", SimpleNamespace(data=due)), cast("UnitOfWork", sweep_uow))

    assert {spec.name: list(spec.event_types) for spec in handlers} == {
        WEBHOOK_HANDLER: ["payments.webhook_received"],
        SWEEPER_HANDLER: ["payments.sweep_due"],
    }
    assert len(uow.after_commit) == 1 and untouched == "requires_payment"
    assert settled == "settled"
    assert sweep_uow.after_commit == [processor.sweep]


async def test_the_sweeper_schedule_follows_the_settings(api: Api, app_engine: AsyncEngine) -> None:
    async def schedule() -> Any:
        async with app_engine.connect() as conn:
            return (
                await conn.execute(
                    text(
                        "SELECT id, event_type, interval_seconds, enabled FROM schedules "
                        "WHERE project_id = :nil AND name = :name"
                    ),
                    {"nil": INTERNAL_PROJECT_ID, "name": SWEEPER_SCHEDULE},
                )
            ).one()

    try:
        async with UnitOfWork(api.factory) as uow:
            first = await ensure_sweeper_schedule(
                uow, PaymentsSettings(sweeper_interval_seconds=30), api.clock
            )
        enabled = await schedule()
    finally:
        # the schedule table is shared by the whole run: never leave a live sweeper behind
        async with UnitOfWork(api.factory) as uow:
            second = await ensure_sweeper_schedule(
                uow, PaymentsSettings(sweeper_enabled=False, sweeper_interval_seconds=90), api.clock
            )
    disabled = await schedule()

    assert first == second == enabled.id
    assert (enabled.event_type, enabled.interval_seconds, enabled.enabled) == (
        "payments.sweep_due",
        30,
        True,
    )
    assert (disabled.interval_seconds, disabled.enabled) == (90, False)
