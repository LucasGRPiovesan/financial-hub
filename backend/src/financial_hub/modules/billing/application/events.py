"""Billing event types (ADR-0005). Payloads carry ids and statuses only: never PII or amounts."""

from financial_hub.modules.events.registry import EventPayload, ScheduleDuePayload, registry


class InvoicePayload(EventPayload):
    invoice_id: str
    subscription_id: str
    customer_id: str
    status: str


class SubscriptionPayload(EventPayload):
    subscription_id: str
    customer_id: str
    plan: str
    status: str


INVOICE_CREATED = registry.register(
    "invoice.created",
    InvoicePayload,
    description="A subscription period was closed into a draft invoice.",
)
INVOICE_FINALIZED = registry.register(
    "invoice.finalized",
    InvoicePayload,
    description="An invoice was finalized: its amounts are fixed and its payment requested.",
)
INVOICE_PAID = registry.register(
    "invoice.paid",
    InvoicePayload,
    description="An invoice was paid (its payment was captured, or nothing was due).",
)
INVOICE_VOIDED = registry.register(
    "invoice.voided",
    InvoicePayload,
    description="An invoice was voided before any payment was captured.",
)
SUBSCRIPTION_CREATED = registry.register(
    "subscription.created",
    SubscriptionPayload,
    description="A customer subscribed to a plan.",
)
SUBSCRIPTION_CANCELED = registry.register(
    "subscription.canceled",
    SubscriptionPayload,
    description="A subscription ended (canceled now, or at the end of its period).",
)


# --- internal: consumed by the billing handler, never sent to customer endpoints ---------------


class BillingCloseDuePayload(ScheduleDuePayload):
    pass


CLOSE_DUE = registry.register(
    "billing.close_due",
    BillingCloseDuePayload,
    description="Internal: the period-close schedule is due (close ended subscription periods).",
)
