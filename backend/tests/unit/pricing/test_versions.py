from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from financial_hub.modules.pricing.domain import (
    FlatFee,
    PerUnit,
    Price,
    PriceBook,
    PriceVersion,
)
from financial_hub.shared.errors import Conflict, NotFound, ValidationFailed
from financial_hub.shared.money import BRL, USD, Money

JAN = datetime(2026, 1, 1, tzinfo=UTC)
FEB = datetime(2026, 2, 1, tzinfo=UTC)
MAR = datetime(2026, 3, 1, tzinfo=UTC)
TICK = timedelta(microseconds=1)
V1 = PerUnit(BRL, Decimal("0.10"))
V2 = PerUnit(BRL, Decimal("0.12"))
V3 = PerUnit(BRL, Decimal("0.15"))


def new_price(code: str = "api_calls", project_id: object = None) -> Price:
    return Price(uuid4(), project_id or uuid4(), code, BRL)  # pyright: ignore[reportArgumentType]


def two_versions() -> Price:
    return new_price().add_version(uuid4(), V1, JAN, FEB).add_version(uuid4(), V2, FEB)


def test_version_selection_at_boundaries() -> None:
    price = two_versions()
    assert price.version_at(JAN).version == 1
    assert price.version_at(FEB - TICK).version == 1
    assert price.version_at(FEB).version == 2  # effective_to is exclusive
    assert price.version_at(datetime(2030, 1, 1, tzinfo=UTC)).version == 2
    with pytest.raises(NotFound):
        price.version_at(JAN - TICK)


def test_version_selection_normalizes_time_zones() -> None:
    price = two_versions()
    sao_paulo = timezone(timedelta(hours=-3))
    # 2026-01-31T21:00-03:00 is FEB in UTC
    assert price.version_at(datetime(2026, 1, 31, 21, tzinfo=sao_paulo)).version == 2
    assert price.versions[0].effective_from.tzinfo is UTC


def test_selection_with_a_gap_between_versions() -> None:
    price = new_price().add_version(uuid4(), V1, JAN, FEB).add_version(uuid4(), V2, MAR)
    with pytest.raises(NotFound):
        price.version_at(FEB)
    assert price.version_at(MAR).version == 2


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (JAN, None),  # open-ended starting at the same instant
        (FEB - TICK, None),  # starts inside v1
        (datetime(2025, 12, 1, tzinfo=UTC), JAN + TICK),  # ends inside v1
        (datetime(2025, 12, 1, tzinfo=UTC), None),  # contains v1
        (JAN + TICK, FEB - TICK),  # inside v1
    ],
)
def test_overlapping_versions_are_rejected(start: datetime, end: datetime | None) -> None:
    price = new_price().add_version(uuid4(), V1, JAN, FEB)
    with pytest.raises(Conflict, match="overlaps"):
        price.add_version(uuid4(), V2, start, end)


def test_adjacent_versions_do_not_overlap() -> None:
    price = new_price().add_version(uuid4(), V1, FEB, MAR)
    price = price.add_version(uuid4(), V2, JAN, FEB).add_version(uuid4(), V3, MAR)
    assert [price.version_at(at).version for at in (JAN, FEB, MAR)] == [2, 1, 3]


def test_open_ended_version_rejects_any_later_version() -> None:
    price = new_price().add_version(uuid4(), V1, JAN)
    with pytest.raises(Conflict):
        price.add_version(uuid4(), V2, MAR)


def test_adding_a_version_never_mutates_existing_ones() -> None:
    first = new_price().add_version(uuid4(), V1, JAN, FEB)
    second = first.add_version(uuid4(), V2, FEB)
    assert len(first.versions) == 1
    assert second.versions[0] is first.versions[0]
    assert [v.version for v in second.versions] == [1, 2]
    assert second.latest is second.versions[1]
    assert new_price().latest is None
    with pytest.raises(FrozenInstanceError):
        second.versions[0].effective_to = MAR  # pyright: ignore[reportAttributeAccessIssue]
    with pytest.raises(FrozenInstanceError):
        second.versions = ()  # pyright: ignore[reportAttributeAccessIssue]


def test_close_open_version_only_in_the_future() -> None:
    price = new_price().add_version(uuid4(), V1, JAN)
    closed = price.close_open_version(MAR, now=FEB)
    assert price.versions[0].effective_to is None  # original value untouched
    assert closed.versions[0].effective_to == MAR
    assert closed.versions[0].id == price.versions[0].id
    follow_up = closed.add_version(uuid4(), V2, MAR)
    assert follow_up.version_at(MAR - TICK).version == 1
    assert follow_up.version_at(MAR).version == 2
    with pytest.raises(Conflict, match="future"):
        price.close_open_version(FEB, now=MAR)
    with pytest.raises(Conflict, match="no open-ended"):
        closed.close_open_version(datetime(2027, 1, 1, tzinfo=UTC), now=MAR)


def test_closing_before_the_version_starts_is_rejected() -> None:
    price = new_price().add_version(uuid4(), V1, MAR)
    with pytest.raises(ValidationFailed, match="after effective_from"):
        price.close_open_version(FEB, now=JAN)


def test_version_invariants() -> None:
    price_id = uuid4()
    with pytest.raises(ValidationFailed, match="after effective_from"):
        PriceVersion(uuid4(), price_id, 1, V1, FEB, JAN)
    with pytest.raises(ValidationFailed, match=">= 1"):
        PriceVersion(uuid4(), price_id, 0, V1, JAN)
    with pytest.raises(TypeError):
        PriceVersion(uuid4(), price_id, True, V1, JAN)
    with pytest.raises(TypeError):
        PriceVersion(uuid4(), price_id, 1, "per_unit", JAN)  # pyright: ignore[reportArgumentType]
    with pytest.raises(ValidationFailed, match="timezone-aware"):
        PriceVersion(uuid4(), price_id, 1, V1, datetime(2026, 1, 1))
    with pytest.raises(TypeError):
        PriceVersion(uuid4(), price_id, 1, V1, "2026-01-01")  # pyright: ignore[reportArgumentType]


def test_price_invariants() -> None:
    price = new_price()
    other = PriceVersion(uuid4(), uuid4(), 1, V1, JAN)
    with pytest.raises(ValidationFailed, match="another price"):
        Price(price.id, price.project_id, "x", BRL, (other,))
    gap = PriceVersion(uuid4(), price.id, 2, V1, JAN)
    with pytest.raises(ValidationFailed, match="numbered"):
        Price(price.id, price.project_id, "x", BRL, (gap,))
    with pytest.raises(ValidationFailed, match="USD"):
        price.add_version(uuid4(), FlatFee(USD, Money(100, USD)), JAN)
    for code in ("", "Upper", "-lead", "a b", "x" * 101):
        with pytest.raises(ValidationFailed, match="price code"):
            new_price(code)


def test_price_book_resolves_by_code_and_instant() -> None:
    project_id = uuid4()
    calls = new_price("api_calls", project_id).add_version(uuid4(), V1, JAN)
    seats = new_price("seats", project_id).add_version(uuid4(), V2, FEB)
    book = PriceBook(project_id, (calls, seats))
    assert book.resolve("seats", MAR).model == V2
    assert book.price("api_calls") is calls
    with pytest.raises(NotFound):
        book.price("missing")
    with pytest.raises(NotFound):
        book.resolve("seats", JAN)

    updated = calls.close_open_version(MAR, now=FEB).add_version(uuid4(), V3, MAR)
    newer = book.with_price(updated)
    assert newer.resolve("api_calls", MAR).model == V3
    assert book.resolve("api_calls", MAR).model == V1  # the old book is unchanged
    assert len(newer.prices) == 2


def test_price_book_invariants() -> None:
    project_id = uuid4()
    calls = new_price("api_calls", project_id)
    with pytest.raises(ValidationFailed, match="another project"):
        PriceBook(project_id, (new_price("x"),))
    with pytest.raises(Conflict, match="twice"):
        PriceBook(project_id, (calls, new_price("api_calls", project_id)))
    with pytest.raises(Conflict, match="twice"):
        PriceBook(project_id, (calls, calls))
