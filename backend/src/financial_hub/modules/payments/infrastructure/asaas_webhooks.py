"""Asaas webhook notifications: parsing and the event-type mapping (W4.4, docs/INTEGRATIONS.md s2).

An Asaas notification is ``{"id": "evt_...", "event": "PAYMENT_RECEIVED", "dateCreated": ...,
"payment": {"object": "payment", "id": "pay_...", "externalReference": "pi_...", ...}}``. The
hub reads only the event id (the dedup key), the event type, and the payment's id and external
reference. Everything else (``value``, ``netValue``, ``status``, customer fields) is ignored on
purpose: processing refetches the payment (ADR-0012 s2). Numbers are parsed as ``Decimal`` so no
``float`` is ever created, even for fields the hub then throws away.

Mapping (``WebhookAction``):

- ``REFRESH``: every documented payment lifecycle event, including refunds, chargebacks
  and ``PAYMENT_DUNNING_RECEIVED`` (a dunning that settles the payment changes its status);
- ``IGNORE``: events that never change what the hub records (an advance on receivables, a
  dunning request, a boleto or checkout page view);
- ``PARK``: everything else, unknown types included (an undone cash receipt, splits, transfers).
  The row is kept for review and the request is acknowledged, so Asaas does not pause the queue
  over a type the hub does not handle yet.
"""

import json
import re
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Final, cast

from financial_hub.modules.payments.domain import (
    ParsedNotification,
    WebhookAction,
    WebhookPayloadInvalid,
)

REFRESH_EVENTS: Final = frozenset(
    {
        "PAYMENT_CREATED",
        "PAYMENT_AUTHORIZED",
        "PAYMENT_AWAITING_RISK_ANALYSIS",
        "PAYMENT_APPROVED_BY_RISK_ANALYSIS",
        "PAYMENT_REPROVED_BY_RISK_ANALYSIS",
        "PAYMENT_CONFIRMED",
        "PAYMENT_RECEIVED",
        "PAYMENT_OVERDUE",
        "PAYMENT_DELETED",
        "PAYMENT_RESTORED",
        "PAYMENT_UPDATED",
        "PAYMENT_BANK_SLIP_CANCELLED",
        "PAYMENT_CREDIT_CARD_CAPTURE_REFUSED",
        "PAYMENT_REFUND_IN_PROGRESS",
        "PAYMENT_REFUNDED",
        "PAYMENT_PARTIALLY_REFUNDED",
        "PAYMENT_REFUND_DENIED",
        "PAYMENT_CHARGEBACK_REQUESTED",
        "PAYMENT_CHARGEBACK_DISPUTE",
        "PAYMENT_AWAITING_CHARGEBACK_REVERSAL",
        "PAYMENT_DUNNING_RECEIVED",
    }
)
IGNORED_EVENTS: Final = frozenset(
    {
        "PAYMENT_ANTICIPATED",
        "PAYMENT_DUNNING_REQUESTED",
        "PAYMENT_BANK_SLIP_VIEWED",
        "PAYMENT_CHECKOUT_VIEWED",
    }
)

# Real ids look like "evt_05b7...f621&368604920": any printable ASCII without whitespace.
_EVENT_ID: Final = re.compile(r"[!-~]{1,200}")
_EVENT_TYPE: Final = re.compile(r"[A-Z0-9_]{1,100}")
_PAYMENT_ID: Final = re.compile(r"[A-Za-z0-9_]{1,64}")
_REFERENCE: Final = re.compile(r"[A-Za-z0-9_.:-]{1,200}")


def classify(event_type: str) -> WebhookAction:
    if event_type in REFRESH_EVENTS:
        return WebhookAction.REFRESH
    if event_type in IGNORED_EVENTS:
        return WebhookAction.IGNORE
    return WebhookAction.PARK


def _text(payload: Mapping[str, Any], name: str, pattern: re.Pattern[str]) -> str | None:
    value = payload.get(name)
    if isinstance(value, str) and pattern.fullmatch(value):
        return value
    return None


def parse_notification(body: bytes | str) -> ParsedNotification:
    """The notification's ids and action; :class:`WebhookPayloadInvalid` if it is not one."""
    try:
        payload = json.loads(body, parse_float=Decimal, parse_constant=_no_constant)
    except ValueError, RecursionError:
        raise WebhookPayloadInvalid("the body is not JSON") from None
    if not isinstance(payload, dict):
        raise WebhookPayloadInvalid("the body is not a JSON object")
    data = cast("dict[str, Any]", payload)
    event_id = _text(data, "id", _EVENT_ID)
    event_type = _text(data, "event", _EVENT_TYPE)
    if event_id is None or event_type is None:
        raise WebhookPayloadInvalid("the notification has no valid id or event")
    action = classify(event_type)
    payment = data.get("payment")
    payment_id = reference = None
    if isinstance(payment, dict):
        fields = cast("dict[str, Any]", payment)
        payment_id = _text(fields, "id", _PAYMENT_ID)
        reference = _text(fields, "externalReference", _REFERENCE)
    if action is WebhookAction.REFRESH and payment_id is None:
        # a payment event that names no payment cannot be refetched: keep it for review
        action = WebhookAction.PARK
    return ParsedNotification(
        provider_event_id=event_id,
        event_type=event_type,
        action=action,
        provider_payment_id=payment_id,
        external_reference=reference,
    )


def _no_constant(name: str) -> object:
    raise ValueError(f"invalid JSON constant {name}")
