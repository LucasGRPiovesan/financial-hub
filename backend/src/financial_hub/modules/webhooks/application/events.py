"""Event types the webhooks module publishes (registered in the canonical registry)."""

from financial_hub.modules.events.registry import EventPayload, registry

WEBHOOK_TEST = "webhook.test"


class WebhookTestPayload(EventPayload):
    endpoint_id: str
    message: str


registry.register(
    WEBHOOK_TEST,
    WebhookTestPayload,
    description="A test ping sent to one webhook endpoint on request (never fanned out).",
)
