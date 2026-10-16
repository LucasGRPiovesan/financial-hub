"""Public facade of the event log (ADR-0005). Other modules import from here or ``registry``."""

from financial_hub.modules.events.application.config import EventsSettings
from financial_hub.modules.events.application.dispatcher import (
    Dispatcher,
    DispatcherConfig,
    Outcome,
)
from financial_hub.modules.events.application.handlers import (
    Handler,
    HandlerRegistry,
    HandlerSpec,
    handlers,
)
from financial_hub.modules.events.application.metrics import EventsMetrics
from financial_hub.modules.events.application.publisher import (
    EventDraft,
    EventPublisher,
    Notifier,
    current_traceparent,
)
from financial_hub.modules.events.application.runtime import EventsRuntime, build_runtime
from financial_hub.modules.events.application.scheduler import LEADER_LOCK_CLASS, Scheduler
from financial_hub.modules.events.application.service import (
    MAX_PAGE_SIZE,
    EventPage,
    create_webhook_subscription,
    ensure_internal_subscription,
    list_events_after,
    replay_delivery,
    set_subscription_enabled,
    set_subscription_event_types,
    sync_handler_subscriptions,
    upsert_schedule,
)
from financial_hub.modules.events.application.webhook_queue import (
    claim_webhook_deliveries,
    complete_webhook_delivered,
    complete_webhook_failed,
    release_webhook_delivery,
    webhook_delivery_endpoint,
)
from financial_hub.modules.events.domain import (
    ClaimedDelivery,
    ClaimedWebhookDelivery,
    DeliveryStatus,
    Event,
    EventCursor,
    RetryPolicy,
    SubscriptionKind,
)

__all__ = [
    "LEADER_LOCK_CLASS",
    "MAX_PAGE_SIZE",
    "ClaimedDelivery",
    "ClaimedWebhookDelivery",
    "DeliveryStatus",
    "Dispatcher",
    "DispatcherConfig",
    "Event",
    "EventCursor",
    "EventDraft",
    "EventPage",
    "EventPublisher",
    "EventsMetrics",
    "EventsRuntime",
    "EventsSettings",
    "Handler",
    "HandlerRegistry",
    "HandlerSpec",
    "Notifier",
    "Outcome",
    "RetryPolicy",
    "Scheduler",
    "SubscriptionKind",
    "build_runtime",
    "claim_webhook_deliveries",
    "complete_webhook_delivered",
    "complete_webhook_failed",
    "create_webhook_subscription",
    "current_traceparent",
    "ensure_internal_subscription",
    "handlers",
    "list_events_after",
    "release_webhook_delivery",
    "replay_delivery",
    "set_subscription_enabled",
    "set_subscription_event_types",
    "sync_handler_subscriptions",
    "upsert_schedule",
    "webhook_delivery_endpoint",
]
