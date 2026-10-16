"""Payment event types (ADR-0005). Payloads carry ids and statuses only: never PII or amounts."""

from financial_hub.modules.events.registry import EventPayload, ScheduleDuePayload, registry


class PaymentCreatedPayload(EventPayload):
    payment_id: str
    customer_id: str
    status: str


class PaymentStatusChangedPayload(EventPayload):
    payment_id: str
    previous_status: str
    status: str


class RefundRequestedPayload(EventPayload):
    refund_id: str
    payment_id: str


PAYMENT_CREATED = registry.register(
    "payment.created",
    PaymentCreatedPayload,
    description="A payment intent was accepted; the provider payment is being created.",
)
PAYMENT_STATUS_CHANGED = registry.register(
    "payment.status_changed",
    PaymentStatusChangedPayload,
    description="The derived public status of a payment changed (fetch the payment for details).",
)
REFUND_REQUESTED = registry.register(
    "refund.requested",
    RefundRequestedPayload,
    description="A refund of a payment was requested from the provider.",
)


# --- internal (W4.4): consumed by the payments handlers, never sent to customer endpoints -------


class WebhookReceivedPayload(EventPayload):
    webhook_id: str


class PaymentsSweepDuePayload(ScheduleDuePayload):
    pass


WEBHOOK_RECEIVED = registry.register(
    "payments.webhook_received",
    WebhookReceivedPayload,
    description="Internal: a provider notification was stored and awaits processing.",
)
SWEEP_DUE = registry.register(
    "payments.sweep_due",
    PaymentsSweepDuePayload,
    description="Internal: the payments sweeper schedule is due (unknown outcomes, webhooks).",
)
