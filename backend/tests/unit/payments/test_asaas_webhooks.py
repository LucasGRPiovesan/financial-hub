"""Asaas webhook parsing, the event-type mapping and the token compare (W4.4)."""

import json
from dataclasses import astuple
from decimal import Decimal

import pytest

from financial_hub.modules.payments.application.webhooks import token_matches
from financial_hub.modules.payments.domain import WebhookAction, WebhookPayloadInvalid
from financial_hub.modules.payments.infrastructure.asaas_webhooks import (
    IGNORED_EVENTS,
    REFRESH_EVENTS,
    classify,
    parse_notification,
)

# The documented example id (docs.asaas.com, "payment events"): note the "&".
EVENT_ID = "evt_05b708f961d739ea7eba7e4db318f621&368604920"


def body(**overrides: object) -> str:
    values: dict[str, object] = {
        "id": EVENT_ID,
        "event": "PAYMENT_RECEIVED",
        "dateCreated": "2026-10-16 16:45:03",
        "payment": {
            "object": "payment",
            "id": "pay_080225913252",
            "externalReference": "pi_0000000000000000000000",
            "value": 10.5,
            "netValue": 9.51,
        },
    }
    values.update(overrides)
    return json.dumps(values)


# --- mapping --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("event_type", "action"),
    [
        ("PAYMENT_CREATED", WebhookAction.REFRESH),
        ("PAYMENT_CONFIRMED", WebhookAction.REFRESH),
        ("PAYMENT_RECEIVED", WebhookAction.REFRESH),
        ("PAYMENT_OVERDUE", WebhookAction.REFRESH),
        ("PAYMENT_DELETED", WebhookAction.REFRESH),
        ("PAYMENT_RESTORED", WebhookAction.REFRESH),
        ("PAYMENT_REFUNDED", WebhookAction.REFRESH),
        ("PAYMENT_PARTIALLY_REFUNDED", WebhookAction.REFRESH),
        ("PAYMENT_REFUND_IN_PROGRESS", WebhookAction.REFRESH),
        ("PAYMENT_CHARGEBACK_REQUESTED", WebhookAction.REFRESH),
        ("PAYMENT_RECEIVED_IN_CASH_UNDONE", WebhookAction.PARK),
        ("PAYMENT_DUNNING_RECEIVED", WebhookAction.REFRESH),
        ("PAYMENT_ANTICIPATED", WebhookAction.IGNORE),
        ("PAYMENT_DUNNING_REQUESTED", WebhookAction.IGNORE),
        ("PAYMENT_BANK_SLIP_VIEWED", WebhookAction.IGNORE),
        ("PAYMENT_CHECKOUT_VIEWED", WebhookAction.IGNORE),
        ("PAYMENT_SPLIT_CANCELLED", WebhookAction.PARK),
        ("CUSTOMER_CREATED", WebhookAction.PARK),
        ("SOMETHING_NEW", WebhookAction.PARK),
    ],
)
def test_each_event_type_maps_to_one_action(event_type: str, action: WebhookAction) -> None:
    assert classify(event_type) is action


def test_refresh_and_ignore_sets_do_not_overlap() -> None:
    assert not REFRESH_EVENTS & IGNORED_EVENTS
    assert all(name.startswith("PAYMENT_") for name in REFRESH_EVENTS | IGNORED_EVENTS)


# --- parsing --------------------------------------------------------------------------------------


def test_a_notification_yields_only_ids_and_the_action() -> None:
    parsed = parse_notification(body())

    assert parsed.provider_event_id == EVENT_ID
    assert parsed.event_type == "PAYMENT_RECEIVED"
    assert parsed.action is WebhookAction.REFRESH
    assert parsed.provider_payment_id == "pay_080225913252"
    assert parsed.external_reference == "pi_0000000000000000000000"
    # amounts are never part of what the hub reads from a notification
    assert not any(isinstance(value, (float, Decimal)) for value in astuple(parsed))


def test_bytes_are_accepted_like_text() -> None:
    assert parse_notification(body().encode()).provider_event_id == EVENT_ID


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        '"text"',
        json.dumps({"event": "PAYMENT_RECEIVED"}),
        json.dumps({"id": EVENT_ID}),
        json.dumps({"id": "", "event": "PAYMENT_RECEIVED"}),
        json.dumps({"id": "evt with space", "event": "PAYMENT_RECEIVED"}),
        json.dumps({"id": "x" * 201, "event": "PAYMENT_RECEIVED"}),
        json.dumps({"id": 42, "event": "PAYMENT_RECEIVED"}),
        json.dumps({"id": EVENT_ID, "event": "payment_received"}),
        '{"id": "evt_1", "event": "PAYMENT_RECEIVED", "payment": {"value": NaN}}',
        '{"id": "evt_1", "event": "PAYMENT_RECEIVED", "payment": {"value": Infinity}}',
    ],
)
def test_anything_but_a_notification_is_refused(raw: str) -> None:
    with pytest.raises(WebhookPayloadInvalid):
        parse_notification(raw)


def test_a_payment_event_without_a_usable_payment_id_is_parked() -> None:
    no_payment = parse_notification(body(payment=None))
    odd_id = parse_notification(body(payment={"id": "pay_1; DROP", "externalReference": "pi_1"}))

    assert no_payment.action is WebhookAction.PARK and no_payment.provider_payment_id is None
    assert odd_id.action is WebhookAction.PARK and odd_id.provider_payment_id is None


def test_an_invalid_external_reference_is_dropped_not_fatal() -> None:
    parsed = parse_notification(body(payment={"id": "pay_1", "externalReference": "a b"}))

    assert parsed.action is WebhookAction.REFRESH
    assert parsed.provider_payment_id == "pay_1" and parsed.external_reference is None


def test_an_ignored_type_keeps_its_ids() -> None:
    parsed = parse_notification(body(event="PAYMENT_BANK_SLIP_VIEWED"))

    assert parsed.action is WebhookAction.IGNORE and parsed.provider_payment_id is not None


# --- token compare --------------------------------------------------------------------------------

TOKEN = "whk_" + "a" * 40


@pytest.mark.parametrize(
    ("presented", "expected", "matches"),
    [
        (TOKEN, TOKEN, True),
        (TOKEN, TOKEN[:-1] + "b", False),
        (TOKEN[:-1], TOKEN, False),
        (TOKEN + "a", TOKEN, False),
        ("", TOKEN, False),
        (None, TOKEN, False),
        (TOKEN, None, False),
        (None, None, False),
        ("", "", False),  # an empty configured token never authenticates an absent header
    ],
)
def test_token_matches_only_the_exact_configured_token(
    presented: str | None, expected: str | None, matches: bool
) -> None:
    assert token_matches(presented, expected) is matches


def test_non_ascii_tokens_compare_without_errors() -> None:
    assert token_matches("çãõ" * 12, "çãõ" * 12) is True
    assert token_matches("\udcff" * 40, TOKEN) is False
