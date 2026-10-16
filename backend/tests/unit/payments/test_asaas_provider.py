"""The Asaas Sandbox adapter against a scripted Asaas (``httpx.MockTransport``): no network.

Covers every port method, every error class of the mapping table (docs/INTEGRATIONS.md), the
decimal edge cases of the money conversion and that the API key never leaves the request header.
"""

import json
import logging
import traceback
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
import pytest
import structlog
from structlog.typing import EventDict

from financial_hub.modules.payments.domain import (
    CaptureStatus,
    CreatePaymentRequest,
    DisputeStatus,
    PaymentMethod,
    ProviderCredentials,
    ProviderCustomerRequest,
    ProviderDuplicate,
    ProviderError,
    ProviderRejected,
    ProviderRetryable,
    ProviderUnavailable,
    ProviderUncertain,
    RefundRequest,
    RefundState,
    SettlementStatus,
)
from financial_hub.modules.payments.infrastructure import asaas_provider
from financial_hub.modules.payments.infrastructure.asaas_provider import (
    MAX_MINOR_UNITS,
    AsaasPaymentProvider,
    asaas_factory,
    from_asaas_value,
    redact_headers,
    to_asaas_value,
)
from financial_hub.shared.money import Currency, Money

BRL = Currency.of("BRL")
KEY = "fake-asaas-key-0f3a9c1d7b5e"
PI = "pi_01hxyzabc"
RF = "rf_01hxyzdef"
BASE = "/v3"
JSON_HEADERS = {"Content-Type": "application/json"}

Responder = httpx.Response | Exception


def reply(status: int = 200, body: object = None, *, text: str | None = None) -> httpx.Response:
    content = text if text is not None else json.dumps(body)
    return httpx.Response(status, content=content.encode(), headers=JSON_HEADERS)


def page(*items: dict[str, Any], has_more: bool = False) -> httpx.Response:
    return reply(
        body={"object": "list", "hasMore": has_more, "data": list(items), "limit": 10, "offset": 0}
    )


def pay(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "object": "payment",
        "id": "pay_001",
        "status": "PENDING",
        "value": 10.0,
        "netValue": 9.01,
        "externalReference": PI,
        "billingType": "PIX",
        "deleted": False,
    }
    values.update(overrides)
    return values


class Asaas:
    """A scripted Asaas: ``on(method, path, *responses)``; the last response repeats."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], list[Responder]] = {}
        self.requests: list[httpx.Request] = []

    def on(self, method: str, path: str, *responses: Responder) -> Asaas:
        self.routes[(method, f"{BASE}/{path}")] = list(responses)
        return self

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        queue = self.routes.get((request.method, request.url.path))
        assert queue, f"unexpected call {request.method} {request.url.path}"
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    def calls(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if (r.method, r.url.path) == (method, f"{BASE}/{path}")]

    def provider(self) -> AsaasPaymentProvider:
        return AsaasPaymentProvider(KEY, transport=httpx.MockTransport(self))


def payment_request(
    amount: int = 1000,
    *,
    method: PaymentMethod = PaymentMethod.PIX,
    customer: str | None = "cus_asaas_1",
    currency: Currency = BRL,
    description: str | None = "Order 1",
) -> CreatePaymentRequest:
    return CreatePaymentRequest(
        external_reference=PI,
        amount=Money(amount, currency),
        method=method,
        due_date=date(2026, 10, 20),
        customer_reference="cus_01hxy",
        provider_customer_id=customer,
        description=description,
    )


def body_of(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content, parse_float=Decimal)


# --- money ----------------------------------------------------------------------------------------

MINOR_TO_TEXT = [
    (1, "0.01"),
    (10, "0.10"),
    (99, "0.99"),
    (100, "1.00"),
    (123456789, "1234567.89"),
    (MAX_MINOR_UNITS, "10000000000.00"),
]


@pytest.mark.parametrize(("minor", "text"), MINOR_TO_TEXT)
def test_minor_units_become_exact_decimal_text_and_back(minor: int, text: str) -> None:
    assert to_asaas_value(Money(minor, BRL)) == text
    assert from_asaas_value(Decimal(text)) == Money(minor, BRL)


@pytest.mark.parametrize(("minor", "text"), MINOR_TO_TEXT)
async def test_the_request_carries_the_amount_as_a_json_number_never_a_float(
    minor: int, text: str
) -> None:
    asaas = Asaas().on("POST", "payments", reply(text=json.dumps(pay())))
    asaas.on("GET", "payments", page())

    await asaas.provider().create_payment(payment_request(minor))

    sent = asaas.calls("POST", "payments")[0]
    assert f'"value":{text}' in sent.content.decode()  # the exact literal, no float round trip
    assert body_of(sent)["value"] == Decimal(text)


@pytest.mark.parametrize("amount", [Money(0, BRL), Money(-5, BRL), Money(MAX_MINOR_UNITS + 1, BRL)])
def test_an_out_of_range_request_amount_is_rejected(amount: Money) -> None:
    with pytest.raises(ProviderRejected) as caught:
        to_asaas_value(amount)

    assert caught.value.code == "invalid_amount"


def test_only_brl_is_supported() -> None:
    with pytest.raises(ProviderRejected) as caught:
        to_asaas_value(Money(100, Currency.of("USD")))

    assert caught.value.code == "currency_not_supported"


@pytest.mark.parametrize("value", ["10.005", "0.001", "1.234", "-1", "10000000000.01"])
def test_response_amounts_with_more_than_two_decimals_or_out_of_range_are_refused(
    value: str,
) -> None:
    with pytest.raises(asaas_provider._InvalidResponse):  # pyright: ignore[reportPrivateUsage]
        from_asaas_value(Decimal(value))


@pytest.mark.parametrize("value", [10.5, "10.50", None, True, [1]])
def test_response_amounts_must_be_parsed_decimals_not_floats_or_strings(value: object) -> None:
    with pytest.raises(asaas_provider._InvalidResponse):  # pyright: ignore[reportPrivateUsage]
        from_asaas_value(value)


@pytest.mark.parametrize(("minor", "text"), MINOR_TO_TEXT)
async def test_responses_are_parsed_to_exact_minor_units(minor: int, text: str) -> None:
    raw = f'{{"id":"pay_9","status":"PENDING","value":{text},"externalReference":"{PI}"}}'
    asaas = Asaas().on("GET", "payments/pay_9", reply(text=raw))

    snapshot = await asaas.provider().get_payment("pay_9")

    assert snapshot.amount == Money(minor, BRL)


@pytest.mark.parametrize("literal", ["10.005", '"10.00"', "null", "-1", "1e400"])
async def test_a_malformed_amount_in_a_2xx_response_is_uncertain_not_rounded(
    literal: str,
) -> None:
    raw = f'{{"id":"pay_9","status":"PENDING","value":{literal}}}'
    asaas = Asaas().on("GET", "payments/pay_9", reply(text=raw))

    with pytest.raises(ProviderUncertain) as caught:
        await asaas.provider().get_payment("pay_9")

    assert caught.value.code == "invalid_response"


# --- get_payment and the status table -------------------------------------------------------------


async def test_get_payment_maps_the_payment_and_its_net_value() -> None:
    asaas = Asaas().on("GET", "payments/pay_001", reply(body=pay(status="RECEIVED")))

    snapshot = await asaas.provider().get_payment("pay_001")

    assert snapshot.provider_payment_id == "pay_001"
    assert snapshot.external_reference == PI
    assert snapshot.raw_status == "RECEIVED"
    assert snapshot.amount == Money(1000, BRL)
    assert snapshot.net_amount == 901
    assert snapshot.capture_status is CaptureStatus.CAPTURED
    assert snapshot.settlement_status is SettlementStatus.SETTLED
    assert snapshot.dispute_status is DisputeStatus.NONE
    assert snapshot.refunds == ()


async def test_a_net_value_outside_zero_to_value_is_dropped() -> None:
    asaas = Asaas().on("GET", "payments/pay_001", reply(body=pay(netValue=10.5)))

    assert (await asaas.provider().get_payment("pay_001")).net_amount is None


async def test_a_deleted_payment_is_canceled() -> None:
    asaas = Asaas().on("GET", "payments/pay_001", reply(body=pay(status="PENDING", deleted=True)))

    snapshot = await asaas.provider().get_payment("pay_001")

    assert snapshot.capture_status is CaptureStatus.CANCELED
    assert snapshot.raw_status == "DELETED"


async def test_an_unknown_status_is_pending_keeps_the_raw_value_and_warns(
    log: list[dict[str, Any]],
) -> None:
    asaas = Asaas().on("GET", "payments/pay_001", reply(body=pay(status="BRAND_NEW")))

    snapshot = await asaas.provider().get_payment("pay_001")

    assert snapshot.raw_status == "BRAND_NEW"
    assert snapshot.capture_status is CaptureStatus.PENDING
    assert next(e for e in log if e["event"] == "payments.asaas.unknown_status")["status"] == (
        "BRAND_NEW"
    )


async def test_a_chargeback_status_opens_a_dispute() -> None:
    asaas = Asaas().on("GET", "payments/pay_001", reply(body=pay(status="CHARGEBACK_REQUESTED")))

    snapshot = await asaas.provider().get_payment("pay_001")

    assert snapshot.capture_status is CaptureStatus.CAPTURED
    assert snapshot.dispute_status is DisputeStatus.OPEN


async def test_refunds_of_a_payment_are_read_from_refunds() -> None:
    refunds = [
        {"status": "DONE", "value": 2.5, "description": RF, "dateCreated": "2026-10-16"},
        {"status": "PENDING", "value": 1.0, "description": "manual refund in the dashboard"},
        {"status": "CANCELLED", "value": 3.0, "id": "ref_77"},
    ]
    asaas = Asaas().on(
        "GET", "payments/pay_001", reply(body=pay(status="REFUNDED", refunds=refunds))
    )

    snapshot = await asaas.provider().get_payment("pay_001")

    mine, dashboard, cancelled = snapshot.refunds
    assert (mine.external_reference, mine.amount, mine.status) == (
        RF,
        Money(250, BRL),
        RefundState.DONE,
    )
    assert mine.provider_refund_id == "pay_001:2026-10-16:0"
    assert dashboard.external_reference is None  # free text is never mistaken for our reference
    assert dashboard.status is RefundState.SUBMITTED
    assert (cancelled.provider_refund_id, cancelled.status) == ("ref_77", RefundState.CANCELLED)


@pytest.mark.parametrize("payment_id", ["", "pay/../x", "pay 1", "pay?x=1", "a" * 65])
async def test_ids_are_validated_before_they_reach_a_url(payment_id: str) -> None:
    asaas = Asaas()

    with pytest.raises(ProviderRejected) as caught:
        await asaas.provider().get_payment(payment_id)

    assert caught.value.code == "invalid_id"
    assert asaas.requests == []


# --- lookup_by_external_reference -----------------------------------------------------------------


async def test_lookup_finds_exactly_one_payment_by_external_reference() -> None:
    asaas = Asaas().on("GET", "payments", page(pay(id="pay_7")))

    snapshot = await asaas.provider().lookup_by_external_reference(PI)

    assert snapshot is not None and snapshot.provider_payment_id == "pay_7"
    query = dict(asaas.requests[0].url.params)
    assert query == {"externalReference": PI, "limit": "10"}


async def test_lookup_confirms_absence_only_on_an_empty_complete_page() -> None:
    asaas = Asaas().on("GET", "payments", page())

    assert await asaas.provider().lookup_by_external_reference(PI) is None


async def test_lookup_ignores_items_the_filter_should_have_excluded() -> None:
    other = pay(id="pay_other", externalReference="pi_other")
    asaas = Asaas().on("GET", "payments", page(other))

    assert await asaas.provider().lookup_by_external_reference(PI) is None


async def test_lookup_on_a_truncated_page_without_a_match_is_inconclusive_not_absent() -> None:
    other = pay(id="pay_other", externalReference="pi_other")
    asaas = Asaas().on("GET", "payments", page(other, has_more=True))

    with pytest.raises(ProviderUncertain) as caught:
        await asaas.provider().lookup_by_external_reference(PI)

    assert caught.value.code == "lookup_inconclusive"


async def test_lookup_with_two_matches_is_uncertain_and_logs_ids_only(
    log: list[dict[str, Any]],
) -> None:
    asaas = Asaas().on("GET", "payments", page(pay(id="pay_1"), pay(id="pay_2")))

    with pytest.raises(ProviderUncertain) as caught:
        await asaas.provider().lookup_by_external_reference(PI)

    assert caught.value.code == "ambiguous_external_reference"
    warning = [e for e in log if e["event"] == "payments.asaas.ambiguous_external_reference"]
    assert len(warning) == 1
    assert warning[0]["provider_ids"] == ["pay_1", "pay_2"]
    assert warning[0]["external_reference"] == PI
    assert "Maria" not in json.dumps(warning, default=str)


async def test_lookup_rejects_an_empty_reference_without_calling_asaas() -> None:
    asaas = Asaas()

    with pytest.raises(ProviderRejected):
        await asaas.provider().lookup_by_external_reference("")

    assert asaas.requests == []


# --- create_customer ------------------------------------------------------------------------------

CUSTOMER_REQUEST = ProviderCustomerRequest(
    external_reference="cus_01hxy",
    name="Maria Silva",
    email="maria@example.com",
    phone="(11) 91234-5678",
)


async def test_create_customer_returns_an_existing_customer_without_posting() -> None:
    asaas = Asaas().on(
        "GET", "customers", page({"id": "cus_asaas_9", "externalReference": "cus_01hxy"})
    )

    customer = await asaas.provider().create_customer(CUSTOMER_REQUEST)

    assert customer.provider_customer_id == "cus_asaas_9"
    assert asaas.calls("POST", "customers") == []


async def test_create_customer_posts_a_minimal_body_with_notifications_off() -> None:
    asaas = Asaas().on("GET", "customers", page())
    asaas.on("POST", "customers", reply(body={"id": "cus_asaas_1", "object": "customer"}))

    customer = await asaas.provider().create_customer(CUSTOMER_REQUEST, cpf_cnpj="24971563792")

    assert customer.provider_customer_id == "cus_asaas_1"
    assert body_of(asaas.calls("POST", "customers")[0]) == {
        "name": "Maria Silva",
        "externalReference": "cus_01hxy",
        "notificationDisabled": True,
        "email": "maria@example.com",
        "mobilePhone": "11912345678",
        "cpfCnpj": "24971563792",
    }


async def test_create_customer_omits_a_phone_that_is_not_ten_or_eleven_digits() -> None:
    asaas = Asaas().on("GET", "customers", page())
    asaas.on("POST", "customers", reply(body={"id": "cus_asaas_1"}))
    request = ProviderCustomerRequest(external_reference="cus_1", name="A", phone="+55 123")

    await asaas.provider().create_customer(request)

    assert "mobilePhone" not in body_of(asaas.calls("POST", "customers")[0])


async def test_create_customer_with_two_existing_matches_is_uncertain(
    log: list[dict[str, Any]],
) -> None:
    asaas = Asaas().on(
        "GET",
        "customers",
        page(
            {"id": "cus_a", "externalReference": "cus_01hxy"},
            {"id": "cus_b", "externalReference": "cus_01hxy"},
        ),
    )

    with pytest.raises(ProviderUncertain):
        await asaas.provider().create_customer(CUSTOMER_REQUEST)

    assert asaas.calls("POST", "customers") == []
    assert [e["provider_ids"] for e in log if "provider_ids" in e] == [["cus_a", "cus_b"]]


# --- create_payment -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "billing"), [(PaymentMethod.PIX, "PIX"), (PaymentMethod.BOLETO, "BOLETO")]
)
async def test_create_payment_posts_the_charge_and_returns_its_snapshot(
    method: PaymentMethod, billing: str
) -> None:
    asaas = Asaas().on("GET", "payments", page())
    asaas.on("POST", "payments", reply(body=pay(billingType=billing)))

    snapshot = await asaas.provider().create_payment(payment_request(method=method))

    assert snapshot.provider_payment_id == "pay_001"
    assert snapshot.capture_status is CaptureStatus.PENDING
    assert body_of(asaas.calls("POST", "payments")[0]) == {
        "customer": "cus_asaas_1",
        "billingType": billing,
        "dueDate": "2026-10-20",
        "externalReference": PI,
        "description": "Order 1",
        "value": Decimal("10.00"),
    }
    # the pre-create lookup asked for exactly our reference
    assert asaas.calls("GET", "payments")[0].url.params["externalReference"] == PI


async def test_create_payment_looks_the_customer_up_by_reference_when_not_linked() -> None:
    asaas = Asaas().on("GET", "payments", page())
    asaas.on("GET", "customers", page({"id": "cus_asaas_5", "externalReference": "cus_01hxy"}))
    asaas.on("POST", "payments", reply(body=pay()))

    await asaas.provider().create_payment(payment_request(customer=None))

    assert body_of(asaas.calls("POST", "payments")[0])["customer"] == "cus_asaas_5"
    assert asaas.calls("GET", "customers")[0].url.params["externalReference"] == "cus_01hxy"


async def test_create_payment_without_a_provisioned_customer_is_rejected() -> None:
    asaas = Asaas().on("GET", "payments", page()).on("GET", "customers", page())

    with pytest.raises(ProviderRejected) as caught:
        await asaas.provider().create_payment(payment_request(customer=None))

    assert caught.value.code == "customer_not_provisioned"
    assert asaas.calls("POST", "payments") == []


async def test_card_payments_are_refused_without_calling_asaas() -> None:
    asaas = Asaas()

    with pytest.raises(ProviderRejected) as caught:
        await asaas.provider().create_payment(payment_request(method=PaymentMethod.CARD))

    assert caught.value.code == "method_not_supported"
    assert asaas.requests == []


async def test_an_existing_external_reference_is_a_duplicate_and_nothing_is_posted() -> None:
    asaas = Asaas().on("GET", "payments", page(pay()))

    with pytest.raises(ProviderDuplicate):
        await asaas.provider().create_payment(payment_request())

    assert asaas.calls("POST", "payments") == []


async def test_a_non_brl_payment_is_rejected_before_any_call() -> None:
    asaas = Asaas()

    with pytest.raises(ProviderRejected) as caught:
        await asaas.provider().create_payment(payment_request(currency=Currency.of("USD")))

    assert (caught.value.code, asaas.requests) == ("currency_not_supported", [])


async def test_the_timeout_after_the_post_is_uncertain_and_the_post_is_not_repeated() -> None:
    asaas = Asaas().on("GET", "payments", page())
    asaas.on("POST", "payments", httpx.ReadTimeout("slow"))

    with pytest.raises(ProviderUncertain) as caught:
        await asaas.provider().create_payment(payment_request())

    assert caught.value.code == "timeout"
    assert len(asaas.calls("POST", "payments")) == 1


async def test_a_connect_error_before_the_post_is_retryable() -> None:
    asaas = Asaas().on("GET", "payments", page())
    asaas.on("POST", "payments", httpx.ConnectError("refused"))

    with pytest.raises(ProviderRetryable) as caught:
        await asaas.provider().create_payment(payment_request())

    assert caught.value.code == "connect_error"


async def test_a_description_is_omitted_when_empty() -> None:
    asaas = Asaas().on("GET", "payments", page())
    asaas.on("POST", "payments", reply(body=pay()))

    await asaas.provider().create_payment(payment_request(description=None))

    assert "description" not in body_of(asaas.calls("POST", "payments")[0])


# --- refund ---------------------------------------------------------------------------------------


def refund_request(minor: int = 550) -> RefundRequest:
    return RefundRequest(
        provider_payment_id="pay_001",
        external_reference=RF,
        amount=Money(minor, BRL),
        description="customer asked",
    )


def refunded(*refunds: dict[str, Any]) -> httpx.Response:
    return reply(body=pay(status="RECEIVED", refunds=list(refunds)))


async def test_a_partial_refund_sends_the_decimal_value_and_our_reference() -> None:
    asaas = Asaas().on("GET", "payments/pay_001", refunded())
    asaas.on(
        "POST",
        "payments/pay_001/refund",
        refunded(
            {"status": "PENDING", "value": 5.5, "description": RF, "dateCreated": "2026-10-16"}
        ),
    )

    snapshot = await asaas.provider().refund(refund_request(550))

    assert (snapshot.external_reference, snapshot.amount, snapshot.status) == (
        RF,
        Money(550, BRL),
        RefundState.SUBMITTED,
    )
    sent = asaas.calls("POST", "payments/pay_001/refund")[0]
    assert '"value":5.50' in sent.content.decode()
    # the user's free-text reason is not forwarded: the description is our correlation key
    assert body_of(sent)["description"] == RF


async def test_a_full_refund_value_is_the_whole_amount() -> None:
    asaas = Asaas().on("GET", "payments/pay_001", refunded())
    asaas.on(
        "POST",
        "payments/pay_001/refund",
        refunded({"status": "DONE", "value": 10.0, "description": RF}),
    )

    snapshot = await asaas.provider().refund(refund_request(1000))

    assert (snapshot.amount, snapshot.status) == (Money(1000, BRL), RefundState.DONE)


async def test_a_refund_already_present_is_a_duplicate_and_nothing_is_posted() -> None:
    asaas = Asaas().on(
        "GET", "payments/pay_001", refunded({"status": "DONE", "value": 5.5, "description": RF})
    )

    with pytest.raises(ProviderDuplicate):
        await asaas.provider().refund(refund_request())

    assert asaas.calls("POST", "payments/pay_001/refund") == []


async def test_a_refund_missing_from_the_response_is_uncertain() -> None:
    asaas = Asaas().on("GET", "payments/pay_001", refunded())
    asaas.on("POST", "payments/pay_001/refund", refunded())

    with pytest.raises(ProviderUncertain) as caught:
        await asaas.provider().refund(refund_request())

    assert caught.value.code == "refund_not_confirmed"


async def test_the_timeout_after_the_refund_post_is_uncertain() -> None:
    asaas = Asaas().on("GET", "payments/pay_001", refunded())
    asaas.on("POST", "payments/pay_001/refund", httpx.ReadError("reset"))

    with pytest.raises(ProviderUncertain):
        await asaas.provider().refund(refund_request())


async def test_a_refund_of_an_unknown_payment_is_rejected() -> None:
    asaas = Asaas().on(
        "GET", "payments/pay_001", reply(404, {"errors": [{"code": "invalid_action"}]})
    )

    with pytest.raises(ProviderRejected) as caught:
        await asaas.provider().refund(refund_request())

    assert caught.value.code == "not_found"


# --- cancel ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["PENDING", "OVERDUE"])
async def test_cancel_deletes_a_charge_that_is_still_unpaid(status: str) -> None:
    asaas = Asaas().on("GET", "payments/pay_001", reply(body=pay(status=status)))
    asaas.on("DELETE", "payments/pay_001", reply(body={"deleted": True, "id": "pay_001"}))

    snapshot = await asaas.provider().cancel("pay_001")

    assert snapshot.capture_status is CaptureStatus.CANCELED
    assert snapshot.raw_status == "DELETED"
    assert snapshot.amount == Money(1000, BRL)
    assert len(asaas.calls("DELETE", "payments/pay_001")) == 1


@pytest.mark.parametrize("status", ["RECEIVED", "CONFIRMED", "REFUNDED", "CHARGEBACK_REQUESTED"])
async def test_cancel_refuses_a_charge_that_was_paid_and_never_deletes_it(status: str) -> None:
    asaas = Asaas().on("GET", "payments/pay_001", reply(body=pay(status=status)))

    with pytest.raises(ProviderRejected) as caught:
        await asaas.provider().cancel("pay_001")

    assert caught.value.code == "not_cancelable"
    assert asaas.calls("DELETE", "payments/pay_001") == []


async def test_cancel_of_an_already_deleted_charge_is_idempotent() -> None:
    asaas = Asaas().on("GET", "payments/pay_001", reply(body=pay(deleted=True)))

    snapshot = await asaas.provider().cancel("pay_001")

    assert snapshot.capture_status is CaptureStatus.CANCELED
    assert asaas.calls("DELETE", "payments/pay_001") == []


async def test_cancel_not_confirmed_as_deleted_is_rejected() -> None:
    asaas = Asaas().on("GET", "payments/pay_001", reply(body=pay()))
    asaas.on("DELETE", "payments/pay_001", reply(body={"deleted": False, "id": "pay_001"}))

    with pytest.raises(ProviderRejected) as caught:
        await asaas.provider().cancel("pay_001")

    assert caught.value.code == "not_cancelable"


async def test_the_timeout_after_the_delete_is_uncertain() -> None:
    asaas = Asaas().on("GET", "payments/pay_001", reply(body=pay()))
    asaas.on("DELETE", "payments/pay_001", httpx.ReadTimeout("slow"))

    with pytest.raises(ProviderUncertain):
        await asaas.provider().cancel("pay_001")


# --- the error table ------------------------------------------------------------------------------

# (what the transport does, expected class and code) for a READ (GET) and a WRITE (POST)
READ_ERRORS: list[tuple[Responder, type[ProviderError], str]] = [
    (httpx.ConnectError("refused"), ProviderRetryable, "connect_error"),
    (httpx.ConnectTimeout("slow connect"), ProviderRetryable, "connect_error"),
    (httpx.PoolTimeout("busy"), ProviderRetryable, "connect_error"),
    (httpx.ReadTimeout("slow"), ProviderRetryable, "timeout"),
    (httpx.WriteTimeout("slow"), ProviderRetryable, "timeout"),
    (httpx.ReadError("reset"), ProviderRetryable, "connection_error"),
    (httpx.RemoteProtocolError("garbage"), ProviderRetryable, "connection_error"),
    (reply(429, {}), ProviderRetryable, "rate_limited"),
    (reply(500, {}), ProviderRetryable, "provider_unavailable"),
    (reply(502, text="<html>bad gateway</html>"), ProviderRetryable, "provider_unavailable"),
    (reply(503, {}), ProviderRetryable, "provider_unavailable"),
    (reply(302, {}), ProviderRetryable, "unexpected_status"),
    (reply(401, {}), ProviderRejected, "invalid_credentials"),
    (reply(403, {}), ProviderRejected, "forbidden"),
    (reply(404, {}), ProviderRejected, "not_found"),
    (reply(200, text="not json"), ProviderUncertain, "invalid_response"),
    (reply(200, text="[1, 2]"), ProviderUncertain, "invalid_response"),
]
WRITE_ERRORS: list[tuple[Responder, type[ProviderError], str]] = [
    (httpx.ConnectError("refused"), ProviderRetryable, "connect_error"),
    (httpx.ConnectTimeout("slow connect"), ProviderRetryable, "connect_error"),
    (httpx.PoolTimeout("busy"), ProviderRetryable, "connect_error"),
    (httpx.ReadTimeout("slow"), ProviderUncertain, "timeout"),
    (httpx.WriteTimeout("slow"), ProviderUncertain, "timeout"),
    (httpx.ReadError("reset"), ProviderUncertain, "connection_error"),
    (httpx.RemoteProtocolError("garbage"), ProviderUncertain, "connection_error"),
    (reply(429, {}), ProviderRetryable, "rate_limited"),
    (reply(500, {}), ProviderUncertain, "provider_error"),
    (reply(502, text="<html>bad gateway</html>"), ProviderUncertain, "provider_error"),
    (reply(503, {}), ProviderUncertain, "provider_error"),
    (reply(302, {}), ProviderUncertain, "unexpected_status"),
    (reply(401, {}), ProviderRejected, "invalid_credentials"),
    (reply(403, {}), ProviderRejected, "forbidden"),
    (reply(404, {}), ProviderRejected, "not_found"),
    (reply(200, text="not json"), ProviderUncertain, "invalid_response"),
]


@pytest.mark.parametrize(("responder", "error", "code"), READ_ERRORS)
async def test_read_errors_map_to_the_port_errors(
    responder: Responder, error: type[ProviderError], code: str
) -> None:
    asaas = Asaas().on("GET", "payments/pay_001", responder)

    with pytest.raises(error) as caught:
        await asaas.provider().get_payment("pay_001")

    assert type(caught.value) is error
    assert caught.value.code == code


@pytest.mark.parametrize(("responder", "error", "code"), WRITE_ERRORS)
async def test_write_errors_map_to_the_port_errors(
    responder: Responder, error: type[ProviderError], code: str
) -> None:
    asaas = Asaas().on("POST", "customers", responder).on("GET", "customers", page())

    with pytest.raises(error) as caught:
        await asaas.provider().create_customer(CUSTOMER_REQUEST)

    assert type(caught.value) is error
    assert caught.value.code == code


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (
            {"errors": [{"code": "invalid_customer", "description": "CPF 249.715.637-92 invalid"}]},
            "invalid_customer",
        ),
        ({"errors": [{"code": "invalid_value.min", "description": "x"}]}, "invalid_value.min"),
        ({"errors": [{"code": "has spaces and PII maria@example.com"}]}, "invalid_request"),
        ({"errors": [{"code": "x" * 61}]}, "invalid_request"),
        ({"errors": [{"description": "no code"}]}, "invalid_request"),
        ({"errors": []}, "invalid_request"),
        ({"errors": "oops"}, "invalid_request"),
        ([], "invalid_request"),
    ],
)
async def test_a_4xx_is_rejected_with_the_asaas_code_and_never_the_message(
    body: object, code: str
) -> None:
    asaas = Asaas().on("GET", "payments/pay_001", reply(400, body))

    with pytest.raises(ProviderRejected) as caught:
        await asaas.provider().get_payment("pay_001")

    assert caught.value.code == code
    rendered = f"{caught.value!r} {caught.value}"
    assert "249.715" not in rendered and "maria@" not in rendered


async def test_a_4xx_with_an_unreadable_body_is_still_a_clean_rejection() -> None:
    asaas = Asaas().on("GET", "payments/pay_001", reply(422, text="\xff not json"))

    with pytest.raises(ProviderRejected) as caught:
        await asaas.provider().get_payment("pay_001")

    assert caught.value.code == "invalid_request"


# --- request shape and secrets --------------------------------------------------------------------


async def test_every_call_carries_the_key_only_as_a_header_with_explicit_timeouts() -> None:
    asaas = Asaas().on("GET", "payments", page()).on("GET", "payments/pay_001", reply(body=pay()))
    provider = asaas.provider()

    await provider.lookup_by_external_reference(PI)
    await provider.get_payment("pay_001")

    for request in asaas.requests:
        assert request.headers["access_token"] == KEY
        assert request.headers["user-agent"].startswith("financial-hub/")
        assert request.url.host == "api-sandbox.asaas.com"
        assert request.url.scheme == "https"
        assert KEY not in str(request.url)  # URLs reach OpenTelemetry spans
        assert request.extensions["timeout"] == {
            "connect": 5.0,
            "read": 15.0,
            "write": 15.0,
            "pool": 5.0,
        }


async def test_a_request_body_never_contains_the_key() -> None:
    asaas = Asaas().on("GET", "payments", page()).on("POST", "payments", reply(body=pay()))

    await asaas.provider().create_payment(payment_request())

    assert all(KEY.encode() not in r.content for r in asaas.requests)


def test_headers_are_redacted_for_debug_output() -> None:
    redacted = redact_headers(
        {"access_token": KEY, "Authorization": "Bearer x", "Accept": "application/json"}
    )

    assert redacted == {
        "access_token": "[REDACTED]",
        "Authorization": "[REDACTED]",
        "Accept": "application/json",
    }


def test_the_provider_never_prints_its_key() -> None:
    provider = AsaasPaymentProvider(KEY)

    assert KEY not in repr(provider)
    assert KEY not in str(provider)
    assert not hasattr(provider, "__dict__")  # nothing like vars(provider) to dump


FAILURES: list[tuple[str, Responder]] = [
    ("connect", httpx.ConnectError("refused")),
    ("timeout", httpx.ReadTimeout("slow")),
    ("401", reply(401, {"errors": [{"code": "invalid_access_token"}]})),
    ("400", reply(400, {"errors": [{"code": "invalid_customer", "description": "bad"}]})),
    ("500", reply(500, text="boom")),
    ("garbage", reply(200, text="{")),
]


@pytest.mark.parametrize(("_name", "responder"), FAILURES)
async def test_the_key_is_absent_from_logs_errors_and_tracebacks_of_failing_calls(
    _name: str,
    responder: Responder,
    log: list[dict[str, Any]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    asaas = Asaas().on("POST", "customers", responder).on("GET", "customers", page())
    provider = asaas.provider()

    with pytest.raises(ProviderError) as caught:
        await provider.create_customer(CUSTOMER_REQUEST)
    with pytest.raises(ProviderError) as read:
        await Asaas().on("GET", "payments/pay_001", responder).provider().get_payment("pay_001")

    outputs = [caplog.text, json.dumps(log, default=str), repr(provider), str(provider)]
    for error in (caught.value, read.value):
        outputs += [repr(error), str(error), *traceback.format_exception(error)]
    assert log, "the adapter logs at debug: the capture must have seen something"
    for output in outputs:
        assert KEY not in output
    assert all(
        event["headers"]["access_token"] == "[REDACTED]" for event in log if "headers" in event
    )


# --- the factory ----------------------------------------------------------------------------------


def credentials(**values: str) -> ProviderCredentials:
    return ProviderCredentials(values)


def test_the_factory_builds_a_sandbox_adapter() -> None:
    provider = asaas_factory()(uuid4(), credentials(api_key=KEY, environment="sandbox"))

    assert provider.name == "asaas"
    assert KEY not in repr(provider)


@pytest.mark.parametrize(
    ("values", "code"),
    [
        ({"environment": "sandbox"}, "credentials_unavailable"),
        ({"api_key": "", "environment": "sandbox"}, "credentials_unavailable"),
        ({"api_key": KEY, "environment": "production"}, "environment_not_supported"),
        ({"api_key": KEY}, "environment_not_supported"),
    ],
)
def test_the_factory_refuses_missing_keys_and_environments_it_is_not_wired_for(
    values: dict[str, str], code: str
) -> None:
    with pytest.raises(ProviderUnavailable) as caught:
        asaas_factory()(uuid4(), ProviderCredentials(values))

    assert caught.value.code == code
    assert KEY not in str(caught.value)


def test_the_factory_hands_the_transport_to_the_adapter() -> None:
    factory: Callable[..., Any] = asaas_factory(transport=httpx.MockTransport(Asaas()))

    assert factory(uuid4(), credentials(api_key=KEY, environment="sandbox")).name == "asaas"


# --- fixtures -------------------------------------------------------------------------------------


@pytest.fixture
def log(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every event the adapter logs, debug included (the process logger filters by level)."""
    events: list[dict[str, Any]] = []

    def capture(_logger: object, _method: str, event: EventDict) -> EventDict:
        events.append(dict(event))
        raise structlog.DropEvent

    logger = structlog.wrap_logger(
        structlog.PrintLogger(),
        processors=[capture],
        wrapper_class=structlog.make_filtering_bound_logger(logging.NOTSET),
    )
    monkeypatch.setattr(asaas_provider, "logger", logger)
    return events
