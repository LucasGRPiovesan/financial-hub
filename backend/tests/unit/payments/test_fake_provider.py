"""The scriptable fake provider, the provider registry and the setting that enables the fake."""

import asyncio
import base64
from datetime import date

import pytest

from financial_hub.modules.payments.application.registry import ProviderRegistry, build_registry
from financial_hub.modules.payments.domain import (
    CaptureStatus,
    CreatePaymentRequest,
    DisputeStatus,
    PaymentMethod,
    ProviderCredentials,
    ProviderCustomerRequest,
    ProviderDuplicate,
    ProviderRejected,
    ProviderRetryable,
    ProviderUnavailable,
    ProviderUncertain,
    RefundRequest,
    RefundState,
    SettlementStatus,
)
from financial_hub.modules.payments.infrastructure.fake_provider import (
    FakeBehavior,
    FakePaymentProvider,
    FakeRefundBehavior,
)
from financial_hub.platform.settings import Settings
from financial_hub.shared.money import Currency, Money

BRL = Currency.of("BRL")
DB_URL = "postgresql+psycopg://u:p@db:5432/x"


def request(ref: str = "pi_1", amount: int = 1000) -> CreatePaymentRequest:
    return CreatePaymentRequest(
        external_reference=ref,
        amount=Money(amount, BRL),
        method=PaymentMethod.PIX,
        due_date=date(2026, 10, 20),
        customer_reference="cus_1",
        provider_customer_id=None,
        description=None,
    )


async def test_succeed_is_pending_and_ids_are_sequential() -> None:
    fake = FakePaymentProvider()

    first = await fake.create_payment(request("pi_1"))
    second = await fake.create_payment(request("pi_2"))

    assert (first.provider_payment_id, second.provider_payment_id) == (
        "fake_pay_000001",
        "fake_pay_000002",
    )
    assert first.capture_status is CaptureStatus.PENDING
    assert first.net_amount is None
    assert fake.payment_count == 2


async def test_scripted_behaviours_are_consumed_in_order() -> None:
    fake = FakePaymentProvider()
    fake.script(
        FakeBehavior.REJECT,
        FakeBehavior.RETRYABLE,
        FakeBehavior.TIMEOUT_BEFORE_COMMIT,
        FakeBehavior.TIMEOUT_AFTER_COMMIT,
        FakeBehavior.SUCCEED_CAPTURED,
    )

    with pytest.raises(ProviderRejected):
        await fake.create_payment(request("a"))
    with pytest.raises(ProviderRetryable):
        await fake.create_payment(request("b"))
    with pytest.raises(ProviderUncertain):
        await fake.create_payment(request("c"))
    assert fake.payment_count == 0
    with pytest.raises(ProviderUncertain):
        await fake.create_payment(request("d"))
    assert fake.find("d") is not None  # created, then the call failed
    paid = await fake.create_payment(request("e"))

    assert paid.capture_status is CaptureStatus.CAPTURED
    assert paid.net_amount == 1000
    with pytest.raises(ProviderDuplicate):
        await fake.create_payment(request("d"))
    assert fake.payment_count == 2


async def test_lookup_get_and_provider_side_changes() -> None:
    fake = FakePaymentProvider()
    created = await fake.create_payment(request("pi_1"))
    pid = created.provider_payment_id

    assert await fake.lookup_by_external_reference("nope") is None
    captured = fake.capture(pid, fee=99)
    assert (captured.capture_status, captured.net_amount) == (CaptureStatus.CAPTURED, 901)
    assert fake.settle(pid).settlement_status is SettlementStatus.SETTLED
    assert fake.dispute(pid, DisputeStatus.OPEN).dispute_status is DisputeStatus.OPEN
    external = fake.refund_externally(pid, Money(100, BRL))
    fetched = await fake.get_payment(pid)
    assert fetched.refunds == (external,)
    assert (await fake.lookup_by_external_reference("pi_1")) == fetched
    fake.lookup_unavailable = True
    with pytest.raises(ProviderUncertain):
        await fake.lookup_by_external_reference("pi_1")
    with pytest.raises(ProviderRejected):
        await fake.get_payment("missing")
    assert fake.fail((await fake.create_payment(request("x"))).provider_payment_id).capture_status


async def test_refund_behaviours() -> None:
    fake = FakePaymentProvider()
    pid = (await fake.create_payment(request())).provider_payment_id
    fake.script_refunds(
        FakeRefundBehavior.SUBMITTED,
        FakeRefundBehavior.REJECT,
        FakeRefundBehavior.RETRYABLE,
        FakeRefundBehavior.TIMEOUT_AFTER_COMMIT,
    )

    def refund(ref: str) -> RefundRequest:
        return RefundRequest(provider_payment_id=pid, external_reference=ref, amount=Money(1, BRL))

    submitted = await fake.refund(refund("rf_1"))
    assert submitted.status is RefundState.SUBMITTED
    with pytest.raises(ProviderRejected):
        await fake.refund(refund("rf_2"))
    with pytest.raises(ProviderRetryable):
        await fake.refund(refund("rf_3"))
    with pytest.raises(ProviderUncertain):
        await fake.refund(refund("rf_4"))
    with pytest.raises(ProviderDuplicate):
        await fake.refund(refund("rf_1"))
    assert (await fake.refund(refund("rf_5"))).status is RefundState.DONE
    fake.complete_refund(pid, submitted.provider_refund_id)
    statuses = {r.external_reference: r.status for r in (await fake.get_payment(pid)).refunds}
    assert statuses == {
        "rf_1": RefundState.DONE,
        "rf_4": RefundState.DONE,
        "rf_5": RefundState.DONE,
    }
    with pytest.raises(ProviderRejected):
        await fake.refund(RefundRequest("missing", "rf_9", Money(1, BRL)))


async def test_cancel_and_customers() -> None:
    fake = FakePaymentProvider()
    pid = (await fake.create_payment(request())).provider_payment_id
    customer = ProviderCustomerRequest(external_reference="cus_1", name="Test")

    first = await fake.create_customer(customer)
    assert (await fake.create_customer(customer)) == first
    assert (await fake.cancel(pid)).capture_status is CaptureStatus.CANCELED
    with pytest.raises(ProviderRejected):
        await fake.cancel(pid)
    with pytest.raises(ProviderRejected):
        await fake.cancel("missing")
    assert fake.name == "fake"
    assert "cancel" in fake.calls


async def test_hold_pauses_a_create_after_it_took_effect() -> None:
    fake = FakePaymentProvider()
    release = fake.hold()

    call = asyncio.create_task(fake.create_payment(request("pi_h")))
    await fake.held.wait()
    assert fake.find("pi_h") is not None and not call.done()
    release.set()

    assert (await call).external_reference == "pi_h"


# --- registry -----------------------------------------------------------------------------------


def test_registry_resolves_the_fake_only_when_enabled() -> None:
    fake = FakePaymentProvider()
    enabled, disabled = ProviderRegistry(fake=fake), ProviderRegistry()

    assert enabled.resolve("fake", None, None) is fake
    assert enabled.fake is fake and enabled.supports("fake")
    assert disabled.fake is None and not disabled.supports("fake")
    with pytest.raises(ProviderUnavailable) as raised:
        disabled.resolve("fake", None, None)
    assert raised.value.code == "fake_provider_disabled"
    with pytest.raises(ValueError):
        disabled.register("fake", lambda _account, _credentials: fake)


def test_registry_builds_real_adapters_with_credentials() -> None:
    from uuid import uuid4

    built: list[tuple[object, str | None]] = []
    fake = FakePaymentProvider()

    def factory(account: object, credentials: ProviderCredentials) -> FakePaymentProvider:
        built.append((account, credentials.get("api_key")))
        return fake

    registry = ProviderRegistry(factories={"asaas": factory})
    account = uuid4()
    credentials = ProviderCredentials({"api_key": "secret-value"})

    assert registry.supports("asaas") and not registry.supports("stripe")
    assert registry.resolve("asaas", account, credentials) is fake
    assert built == [(account, "secret-value")]
    with pytest.raises(ProviderUnavailable, match="provider_not_supported"):
        registry.resolve("stripe", account, credentials)
    with pytest.raises(ProviderUnavailable, match="credentials_unavailable"):
        registry.resolve("asaas", account, None)
    assert "secret-value" not in repr(credentials) and "secret-value" not in str(credentials)


def settings(**values: object) -> Settings:
    key = base64.b64encode(b"\x01" * 32).decode()
    return Settings(  # pyright: ignore[reportCallIssue]
        _env_file=None,
        database_url=DB_URL,
        encryption_keys={1: key},
        api_key_pepper="unit-test-pepper-0123456789abcdef-0123",
        **values,
    )


@pytest.mark.parametrize(
    ("app_env", "flag", "expected"),
    [
        ("dev", None, True),
        ("test", None, True),
        ("staging", None, False),
        ("prod", None, False),
        ("staging", True, True),
        ("dev", False, False),
    ],
)
def test_the_fake_provider_flag(app_env: str, flag: bool | None, expected: bool) -> None:
    configured = settings(app_env=app_env, payments_fake_provider_enabled=flag)

    assert configured.fake_payment_provider_enabled is expected
    assert build_registry(configured).fake_enabled is expected


def test_the_fake_provider_cannot_be_enabled_in_live() -> None:
    with pytest.raises(ValueError, match="FH_PAYMENTS_FAKE_PROVIDER_ENABLED"):
        settings(app_env="prod", payments_fake_provider_enabled=True)


async def test_a_namespace_keeps_ids_unique_across_processes() -> None:
    fake = FakePaymentProvider(namespace="a1b2")

    created = await fake.create_payment(request())

    assert created.provider_payment_id == "fake_pay_a1b2_000001"
    named = build_registry(settings(app_env="dev")).fake
    assert isinstance(named, FakePaymentProvider) and len(named.namespace) == 8


def test_the_public_api_of_the_module() -> None:
    from financial_hub.modules.payments.application import api

    assert api.ProviderRegistry is ProviderRegistry and api.build_registry is build_registry
    assert set(api.__all__) >= {"PaymentService", "NewPayment", "post_payment_changes"}
