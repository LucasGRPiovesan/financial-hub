"""Integrations event types (ADR-0005). Payloads carry versions and ids, never payload content."""

from financial_hub.modules.events.registry import EventPayload, registry


class ManifestActivatedPayload(EventPayload):
    version: int
    previous_version: int | None
    content_hash: str
    activated_by: str


class BusinessEventRecordedPayload(EventPayload):
    name: str
    idempotency_key: str
    manifest_version: int
    usage_transaction_ids: list[str]


MANIFEST_ACTIVATED = registry.register(
    "manifest.activated",
    ManifestActivatedPayload,
    description=(
        "A manifest version became the project's active integration contract (an activation or "
        "a rollback to an earlier version); `previous_version` is the one it superseded."
    ),
)
BUSINESS_EVENT_RECORDED = registry.register(
    "business_event.recorded",
    BusinessEventRecordedPayload,
    description=(
        "A business event was accepted (once per name and idempotency key) and mapped to the "
        "listed usage events; its payload is not included."
    ),
)
