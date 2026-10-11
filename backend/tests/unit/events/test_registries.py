"""Event-type registry, handler registry, settings and metrics (no database)."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from financial_hub.modules.events.application import (
    EventsMetrics,
    EventsSettings,
    HandlerRegistry,
    RetryPolicy,
)
from financial_hub.modules.events.domain import DeliveryStatus, Event
from financial_hub.modules.events.infrastructure import tables
from financial_hub.modules.events.infrastructure.pg import libpq_dsn
from financial_hub.modules.events.infrastructure.repository import DeliveryStats
from financial_hub.modules.events.registry import (
    SYSTEM_TEST,
    EventPayload,
    EventRegistry,
    SystemTestPayload,
    UnregisteredEventType,
    registry,
)
from financial_hub.platform.db import UnitOfWork
from financial_hub.shared.errors import ValidationFailed

AT = datetime(2026, 10, 10, 12, tzinfo=UTC)


class Payload(EventPayload):
    amount: int


async def noop(_event: Event, _uow: UnitOfWork) -> None:
    return None


# --- event types ---------------------------------------------------------------------------------


def test_registry_resolves_latest_or_exact_versions() -> None:
    types = EventRegistry()
    v1 = types.register("billing.paid", Payload, description=" first ")
    v2 = types.register("billing.paid", Payload, description="second", version=2)
    assert v1.description == "first"
    assert types.get("billing.paid") is v2
    assert types.get("billing.paid", 1) is v1
    assert "billing.paid" in types
    assert "billing.other" not in types
    assert list(types) == [v1, v2]
    with pytest.raises(UnregisteredEventType, match=r"billing.paid v3"):
        types.get("billing.paid", 3)
    with pytest.raises(UnregisteredEventType, match=r"'billing.other'"):
        types.get("billing.other")


@pytest.mark.parametrize(
    ("name", "kwargs", "message"),
    [
        ("Billing.paid", {}, "invalid event type name"),
        ("billing", {}, "invalid event type name"),
        ("billing.paid", {"version": 0}, "version"),
        ("billing.paid", {"description": "  "}, "description"),
    ],
)
def test_registry_rejects_bad_registrations(
    name: str, kwargs: dict[str, Any], message: str
) -> None:
    values: dict[str, Any] = {"description": "ok", **kwargs}
    with pytest.raises(ValueError, match=message):
        EventRegistry().register(name, Payload, **values)


def test_registry_rejects_duplicates() -> None:
    types = EventRegistry()
    types.register("billing.paid", Payload, description="x")
    with pytest.raises(ValueError, match="already registered"):
        types.register("billing.paid", Payload, description="x")


def test_event_type_dumps_and_parses_payloads() -> None:
    assert SYSTEM_TEST.dump({"message": "hi"}) == {"message": "hi"}
    assert SYSTEM_TEST.dump(SystemTestPayload(message="hi")) == {"message": "hi"}
    assert SYSTEM_TEST.parse({"message": "hi"}) == SystemTestPayload(message="hi")
    with pytest.raises(ValidationFailed) as info:
        SYSTEM_TEST.parse({})
    assert [error.pointer for error in info.value.errors] == ["/data/message"]
    with pytest.raises(ValidationFailed, match="expects SystemTestPayload, got Payload"):
        SYSTEM_TEST.dump(Payload(amount=1))
    assert registry.get("system.test") is SYSTEM_TEST


# --- handlers ------------------------------------------------------------------------------------


def test_handler_registry() -> None:
    handlers = HandlerRegistry()
    handlers.register("b.handler", noop, event_types=["system.*"])
    handlers.register("a.handler", noop)
    assert handlers.names == ("a.handler", "b.handler")
    assert len(handlers) == 2
    assert [spec.name for spec in handlers] == ["a.handler", "b.handler"]
    assert next(iter(handlers)).event_types == ()
    assert handlers.get("b.handler") is noop
    assert handlers.get("missing") is None
    with pytest.raises(ValueError, match="already registered"):
        handlers.register("a.handler", noop)
    with pytest.raises(ValueError, match="invalid event type filter"):
        handlers.register("c.handler", noop, event_types=["system."])
    with pytest.raises(ValueError, match="invalid handler name"):
        handlers.register("Bad", noop)


# --- settings ------------------------------------------------------------------------------------


def test_settings_defaults_and_derived_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FH_EVENTS_LEASE_SECONDS", "15")
    monkeypatch.setenv("FH_EVENTS_MAX_ATTEMPTS", "3")
    settings = EventsSettings()
    assert settings.lease == timedelta(seconds=15)
    assert settings.retry_policy == RetryPolicy(
        base_seconds=1.0, cap_seconds=3600.0, max_attempts=3
    )
    assert settings.scheduler_lock_key == 1


def test_libpq_dsn_drops_the_sqlalchemy_driver() -> None:
    assert libpq_dsn("postgresql+psycopg://u:p%40ss@db:5432/x") == "postgresql://u:p%40ss@db:5432/x"


def test_tables_mirror_the_migration() -> None:
    assert {"events", "event_subscriptions", "event_deliveries", "inbox"} <= set(
        tables.events.metadata.tables
    )
    assert str(tables.events.c.txid.type.compile()) == "xid8"


# --- metrics -------------------------------------------------------------------------------------


def snapshot(reader: InMemoryMetricReader) -> dict[str, list[tuple[dict[str, Any], float]]]:
    data = reader.get_metrics_data()
    assert data is not None
    return {
        metric.name: [(dict(p.attributes or {}), p.value) for p in metric.data.data_points]  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownArgumentType]
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }


def test_gauges_are_empty_until_stats_arrive_then_report_them() -> None:
    reader = InMemoryMetricReader()
    events_metrics = EventsMetrics(MeterProvider(metric_readers=[reader]).get_meter("test"))
    events_metrics.fenced.add(1)
    assert events_metrics.lag_seconds is None
    before = snapshot(reader)
    assert "events.delivery.lag" not in before
    assert "events.notification_queue.usage" not in before

    events_metrics.update(
        DeliveryStats(
            oldest_due=AT - timedelta(seconds=30),
            by_status={DeliveryStatus.PENDING: 4, DeliveryStatus.SUCCEEDED: 99},
            notification_queue_usage=0.25,
        ),
        AT,
    )
    after = snapshot(reader)
    assert after["events.delivery.lag"] == [({}, 30.0)]
    assert after["events.deliveries"] == [({"status": "pending"}, 4)]
    assert after["events.notification_queue.usage"] == [({}, 0.25)]

    events_metrics.update(DeliveryStats(None, {}, 0.0), AT)
    assert events_metrics.lag_seconds == 0.0
    events_metrics.update(DeliveryStats(AT + timedelta(seconds=5), {}, 0.0), AT)
    assert events_metrics.lag_seconds == 0.0  # due in the future is not lag
