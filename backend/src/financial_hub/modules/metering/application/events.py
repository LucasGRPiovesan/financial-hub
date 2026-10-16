"""Metering event types (ADR-0005). Payloads carry ids and the measure, never event properties."""

from datetime import datetime

from financial_hub.modules.events.registry import EventPayload, registry


class UsageRecordedPayload(EventPayload):
    transaction_id: str
    customer_id: str
    metric: str
    timestamp: datetime
    value: str | None


USAGE_RECORDED = registry.register(
    "usage.recorded",
    UsageRecordedPayload,
    description=(
        "A usage event was accepted (once per transaction_id). `value` is the exact decimal "
        "measure of a sum/max metric, null otherwise; the event's properties are not included."
    ),
)
