"""OpenTelemetry instruments of the event log (exported via OTLP; names as Prometheus sees them).

- ``dispatcher_fenced_total``: completions discarded because the lease token changed (F-22).
- ``events_notify_failures_total``: wake-up NOTIFYs that failed after commit (polling covers).
- ``events_deliveries_completed_total{handler,outcome}``: succeeded / duplicate / retry / dead.
- ``events_delivery_attempts``: attempt number of each processed delivery (histogram).
- ``events_delivery_lag_seconds``: age of the oldest due delivery.
- ``events_deliveries{status}``: backlog by status (succeeded is not counted: it only grows).
- ``events_notification_queue_usage``: ``pg_notification_queue_usage()`` (0..1).

Gauges read a snapshot the dispatcher refreshes every ``stats_interval_seconds``.
"""

from collections.abc import Iterable
from datetime import datetime

from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Meter, Observation

from financial_hub.modules.events.domain import DeliveryStatus
from financial_hub.modules.events.infrastructure.repository import DeliveryStats

METER_NAME = "financial_hub.events"


class EventsMetrics:
    def __init__(self, meter: Meter | None = None) -> None:
        meter = meter or metrics.get_meter(METER_NAME)
        self._stats: DeliveryStats | None = None
        self._stats_at: datetime | None = None
        self.fenced = meter.create_counter(
            "dispatcher.fenced",
            unit="{delivery}",
            description="Delivery completions discarded because another worker holds the lease.",
        )
        self.notify_failures = meter.create_counter(
            "events.notify.failures",
            unit="{notification}",
            description="Post-commit NOTIFY wake-ups that failed (deliveries still get polled).",
        )
        self.completed = meter.create_counter(
            "events.deliveries.completed",
            unit="{delivery}",
            description="Processed deliveries by handler and outcome.",
        )
        self.attempts = meter.create_histogram(
            "events.delivery.attempts",
            unit="{attempt}",
            description="Attempt number of each processed delivery.",
            explicit_bucket_boundaries_advisory=[1, 2, 3, 5, 8, 13, 21],
        )
        meter.create_observable_gauge(
            "events.delivery.lag",
            callbacks=[self._observe_lag],
            unit="s",
            description="Age of the oldest due, not yet leased delivery.",
        )
        meter.create_observable_gauge(
            "events.deliveries",
            callbacks=[self._observe_backlog],
            unit="{delivery}",
            description="Deliveries by status (succeeded excluded).",
        )
        meter.create_observable_gauge(
            "events.notification_queue.usage",
            callbacks=[self._observe_queue],
            unit="1",
            description="Fraction of the Postgres NOTIFY queue in use.",
        )

    def update(self, stats: DeliveryStats, at: datetime) -> None:
        self._stats, self._stats_at = stats, at

    @property
    def lag_seconds(self) -> float | None:
        if self._stats is None or self._stats_at is None:
            return None
        oldest = self._stats.oldest_due
        return 0.0 if oldest is None else max((self._stats_at - oldest).total_seconds(), 0.0)

    def _observe_lag(self, _options: CallbackOptions) -> Iterable[Observation]:
        lag = self.lag_seconds
        return [] if lag is None else [Observation(lag)]

    def _observe_backlog(self, _options: CallbackOptions) -> Iterable[Observation]:
        if self._stats is None:
            return []
        return [
            Observation(count, {"status": status.value})
            for status, count in self._stats.by_status.items()
            if status is not DeliveryStatus.SUCCEEDED
        ]

    def _observe_queue(self, _options: CallbackOptions) -> Iterable[Observation]:
        if self._stats is None:
            return []
        return [Observation(self._stats.notification_queue_usage)]
