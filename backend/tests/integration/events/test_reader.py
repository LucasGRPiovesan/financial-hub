"""``list_events_after``: (txid, id) paging below the visibility horizon (ADR-0005, F-23)."""

import pytest

from financial_hub.modules.events.application import (
    MAX_PAGE_SIZE,
    EventCursor,
    EventPage,
    list_events_after,
)
from financial_hub.shared.errors import ValidationFailed
from financial_hub.shared.ids import new_uuid
from financial_hub.shared.pagination import InvalidCursorError
from tests.integration.events.support import Harness


async def page(harness: Harness, **kwargs: object) -> EventPage:
    async with harness.uow() as uow:
        return await list_events_after(uow, project_id=harness.project_id, **kwargs)  # pyright: ignore[reportArgumentType]


async def test_pages_in_commit_order_with_opaque_cursors(harness: Harness) -> None:
    published = [await harness.publish(f"m{i}") for i in range(5)]

    first = await page(harness, limit=2)
    assert [e.id for e in first.events] == [e.id for e in published[:2]]
    assert first.next_cursor == published[1].cursor
    assert first.next_cursor is not None
    second = await page(harness, cursor=first.next_cursor.encode(), limit=2)
    assert [e.id for e in second.events] == [e.id for e in published[2:4]]
    third = await page(harness, cursor=second.next_cursor, limit=2)
    assert [e.id for e in third.events] == [published[4].id]
    empty = await page(harness, cursor=third.next_cursor)
    assert empty.events == []
    assert empty.next_cursor == third.next_cursor  # keep your position when caught up

    assert third.events[0].data == {"message": "m4"}
    assert third.events[0].txid == published[4].txid


async def test_filters_by_type_pattern_and_subject(harness: Harness) -> None:
    a = await harness.publish("a", subject="customer:1")
    b = await harness.publish("b", type="system.test_handled", subject="customer:2")
    await harness.publish("other project", project_id=new_uuid())

    assert [e.id for e in (await page(harness)).events] == [a.id, b.id]
    assert [e.id for e in (await page(harness, types=["system.test"])).events] == [a.id]
    assert [e.id for e in (await page(harness, types=["system.*"])).events] == [a.id, b.id]
    assert [e.id for e in (await page(harness, types=["ledger.*"])).events] == []
    assert [e.id for e in (await page(harness, subject="customer:2")).events] == [b.id]


async def test_rejects_bad_cursor_limit_and_filters(harness: Harness) -> None:
    with pytest.raises(InvalidCursorError):
        await page(harness, cursor="not-a-cursor")
    with pytest.raises(ValidationFailed, match="limit"):
        await page(harness, limit=0)
    with pytest.raises(ValidationFailed, match="limit"):
        await page(harness, limit=MAX_PAGE_SIZE + 1)
    with pytest.raises(ValidationFailed, match="filter"):
        await page(harness, types=["Bad"])


async def test_events_of_an_open_transaction_hold_back_later_commits(harness: Harness) -> None:
    """T1 inserts A and stays open; T2 commits B. B must not be readable past A until T1 ends.

    Otherwise a consumer would advance its cursor past B and never see A (a "lost" event).
    """
    before = await harness.publish("before")

    t1 = harness.uow()
    await t1.__aenter__()
    try:
        event_a = await harness.publisher.publish(t1, harness.draft("A"))
        event_b = await harness.publish("B")  # T2: commits first, with a larger txid
        assert event_b.txid > event_a.txid

        visible = await page(harness)
        assert [e.id for e in visible.events] == [before.id]
        after_before = await page(harness, cursor=before.cursor)
        assert after_before.events == []
        assert after_before.next_cursor == before.cursor
    finally:
        await t1.__aexit__(None, None, None)  # commit T1

    caught_up = await page(harness, cursor=before.cursor)
    assert [e.id for e in caught_up.events] == [event_a.id, event_b.id]


async def test_rolled_back_transaction_releases_the_horizon(harness: Harness) -> None:
    t1 = harness.uow()
    await t1.__aenter__()
    await harness.publisher.publish(t1, harness.draft("never"))
    committed = await harness.publish("kept")
    assert (await page(harness)).events == []
    await t1.__aexit__(RuntimeError, RuntimeError("rollback"), None)

    assert [e.id for e in (await page(harness)).events] == [committed.id]


def test_cursor_round_trip() -> None:
    cursor = EventCursor(123, new_uuid())
    assert EventCursor.decode(cursor.encode()) == cursor
