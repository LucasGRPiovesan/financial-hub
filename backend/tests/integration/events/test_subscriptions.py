"""Subscriptions: fan-out is all-or-nothing against concurrent subscription changes."""

import asyncio

import pytest

from financial_hub.modules.events.application import (
    Event,
    HandlerRegistry,
    create_webhook_subscription,
    ensure_internal_subscription,
    set_subscription_enabled,
    sync_handler_subscriptions,
)
from financial_hub.platform.db import UnitOfWork
from financial_hub.shared.errors import NotFound, ValidationFailed
from financial_hub.shared.ids import new_uuid
from tests.integration.events.support import Harness, unique_handler


async def deliveries_by_event(harness: Harness, subscription_id: object) -> dict[object, int]:
    rows = await harness.rows(
        "SELECT e.id, count(d.id) AS n FROM events e "
        "LEFT JOIN event_deliveries d ON d.event_id = e.id AND d.subscription_id = :s "
        "WHERE e.project_id = :p GROUP BY e.id",
        s=subscription_id,
    )
    return {row["id"]: row["n"] for row in rows}


async def test_publishers_wait_for_a_subscription_being_created(harness: Harness) -> None:
    """Events published while a subscription commits all get its delivery (fully)."""
    handler = unique_handler()
    creating = harness.uow()
    await creating.__aenter__()
    try:
        subscription_id = await ensure_internal_subscription(
            creating, handler=handler, event_types=["system.test"], project_id=harness.project_id
        )
        publishers = [asyncio.create_task(harness.publish(f"m{i}")) for i in range(5)]
        await asyncio.sleep(0.3)
        assert not any(task.done() for task in publishers)  # blocked on the fan-out lock
    finally:
        await creating.__aexit__(None, None, None)
    events = await asyncio.gather(*publishers)

    counts = await deliveries_by_event(harness, subscription_id)
    assert {counts[event.id] for event in events} == {1}


async def test_subscription_waits_for_in_flight_publishers(harness: Harness) -> None:
    """An event whose transaction began fan-out first gets no delivery (not at all)."""
    publishing = harness.uow()
    await publishing.__aenter__()
    try:
        in_flight = await harness.publisher.publish(publishing, harness.draft("in flight"))
        create = asyncio.create_task(harness.subscribe())
        await asyncio.sleep(0.3)
        assert not create.done()  # exclusive lock waits for the shared holder
    finally:
        await publishing.__aexit__(None, None, None)
    _, subscription_id = await create
    after = await harness.publish("after")

    counts = await deliveries_by_event(harness, subscription_id)
    assert counts == {in_flight.id: 0, after.id: 1}


async def test_concurrent_publishing_and_subscribing_never_splits_a_transaction(
    harness: Harness,
) -> None:
    """Many two-event transactions race subscription creation: each gets 0 or 2 deliveries."""

    async def publish_pair(i: int) -> tuple[Event, Event]:
        async with harness.uow() as uow:
            first = await harness.publisher.publish(uow, harness.draft(f"{i}a"))
            await asyncio.sleep(0.001)
            second = await harness.publisher.publish(uow, harness.draft(f"{i}b"))
        return first, second

    tasks = [asyncio.create_task(publish_pair(i)) for i in range(20)]
    _, subscription_id = await harness.subscribe()
    pairs = await asyncio.gather(*tasks)
    later = await harness.publish("later")

    counts = await deliveries_by_event(harness, subscription_id)
    for first, second in pairs:
        assert counts[first.id] == counts[second.id]
    assert counts[later.id] == 1


async def test_disabling_a_subscription_stops_fan_out(harness: Harness) -> None:
    _, subscription_id = await harness.subscribe()
    before = await harness.publish()
    async with harness.uow() as uow:
        await set_subscription_enabled(uow, subscription_id, False)
    disabled = await harness.publish()
    async with harness.uow() as uow:
        await set_subscription_enabled(uow, subscription_id, True)
    enabled = await harness.publish()

    counts = await deliveries_by_event(harness, subscription_id)
    assert counts == {before.id: 1, disabled.id: 0, enabled.id: 1}
    with pytest.raises(NotFound):
        async with harness.uow() as uow:
            await set_subscription_enabled(uow, new_uuid(), False)


async def test_ensure_is_idempotent_and_updates_filters(harness: Harness) -> None:
    handler, first = await harness.subscribe(event_types=["ledger.*"])
    _, second = await harness.subscribe(handler, event_types=["system.test", "system.test"])
    assert first == second
    stored = await harness.scalar(
        "SELECT event_types FROM event_subscriptions WHERE id = :s", s=first
    )
    assert stored == ["system.test"]


async def test_webhook_deliveries_wait_for_the_sender(harness: Harness) -> None:
    async with harness.uow() as uow:
        subscription_id = await create_webhook_subscription(
            uow, project_id=harness.project_id, endpoint_id=new_uuid(), event_types=["system.test"]
        )
    await harness.subscribed_handler()
    await harness.publish()

    await harness.dispatcher().run_once()

    kinds = await harness.rows(
        "SELECT s.kind, d.status FROM event_deliveries d "
        "JOIN event_subscriptions s ON s.id = d.subscription_id WHERE d.project_id = :p "
        "ORDER BY s.kind"
    )
    assert kinds == [
        {"kind": "internal", "status": "succeeded"},
        {"kind": "webhook", "status": "pending"},
    ]
    with pytest.raises(ValidationFailed):
        async with harness.uow() as uow:
            await create_webhook_subscription(
                uow, project_id=harness.project_id, endpoint_id=new_uuid(), event_types=["*."]
            )
    assert subscription_id is not None


async def test_sync_creates_global_subscriptions_for_declared_handlers(harness: Harness) -> None:
    registry = HandlerRegistry()
    declared, manual = unique_handler(), unique_handler()
    event_type = f"test.sync_{new_uuid().hex[-8:]}"  # matches nothing anyone publishes

    async def noop(_event: Event, _uow: UnitOfWork) -> None:
        return None

    registry.register(declared, noop, event_types=[event_type])
    registry.register(manual, noop)
    async with harness.uow() as uow:
        (subscription_id,) = await sync_handler_subscriptions(uow, registry)
    try:
        row = (
            await harness.rows(
                "SELECT project_id, handler, kind, event_types FROM event_subscriptions "
                "WHERE id = :s",
                s=subscription_id,
            )
        )[0]
        assert row == {
            "project_id": None,
            "handler": declared,
            "kind": "internal",
            "event_types": [event_type],
        }
    finally:
        async with harness.uow() as uow:
            await set_subscription_enabled(uow, subscription_id, False)
