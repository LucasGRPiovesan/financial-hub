"""Customer event types (ADR-0005). Payloads never carry PII: ids, own ref and flags."""

from financial_hub.modules.events.registry import EventPayload, registry


class CustomerCreatedPayload(EventPayload):
    customer_id: str
    external_ref: str


class CustomerUpdatedPayload(EventPayload):
    customer_id: str
    external_ref: str
    changed: list[str]


class CustomerAnonymizedPayload(EventPayload):
    customer_id: str
    external_ref: str


CUSTOMER_CREATED = registry.register(
    "customer.created",
    CustomerCreatedPayload,
    description="A customer identity was created in the project (no personal data in the payload).",
)
CUSTOMER_UPDATED = registry.register(
    "customer.updated",
    CustomerUpdatedPayload,
    description="A customer identity changed; `changed` lists field names only.",
)
CUSTOMER_ANONYMIZED = registry.register(
    "customer.anonymized",
    CustomerAnonymizedPayload,
    description="A customer identity was anonymized (personal data erased, ledger untouched).",
)
