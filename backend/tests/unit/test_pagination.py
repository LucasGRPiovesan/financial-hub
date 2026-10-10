import base64
import uuid
from datetime import UTC, datetime, timedelta, timezone
from typing import ClassVar

import pytest
from pydantic import ValidationError
from sqlalchemy import Column, DateTime, MetaData, Table, Uuid, select
from sqlalchemy.dialects import postgresql

from financial_hub.shared.errors import ValidationFailed
from financial_hub.shared.pagination import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    Cursor,
    InvalidCursorError,
    Page,
    PageParams,
    build_page,
    decode_cursor,
    encode_cursor,
    keyset_predicate,
    validate_limit,
)

AT = datetime(2026, 10, 10, 12, 30, 15, 123456, tzinfo=UTC)
ROW_ID = uuid.UUID("01890a5d-ac96-774b-bcce-b302099a8057")

rows_table = Table(
    "rows",
    MetaData(),
    Column("id", Uuid, primary_key=True),
    Column("created_at", DateTime(timezone=True)),
)


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class TestCursor:
    def test_round_trip(self) -> None:
        cursor = Cursor(AT, ROW_ID)
        encoded = encode_cursor(cursor)

        assert decode_cursor(encoded) == cursor
        assert "=" not in encoded
        assert set(encoded) <= set(
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        )

    def test_round_trip_preserves_offset(self) -> None:
        local = AT.astimezone(timezone(timedelta(hours=-3)))

        decoded = decode_cursor(encode_cursor(Cursor(local, ROW_ID)))

        assert decoded.created_at == AT
        assert decoded.created_at.utcoffset() == timedelta(hours=-3)

    def test_naive_datetime_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            Cursor(datetime(2026, 10, 10), ROW_ID)

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "not base64 !!",
            "x",
            b64(b"\xff\xfe"),
            b64(b"{not json"),
            b64(b'{"a": 1}'),
            b64(b'["2026-10-10T12:00:00+00:00"]'),
            b64(b"[1, 2]"),
            b64(b'["not a date", "01890a5d-ac96-774b-bcce-b302099a8057"]'),
            b64(b'["2026-10-10T12:00:00+00:00", "not-a-uuid"]'),
            b64(b'["2026-10-10T12:00:00", "01890a5d-ac96-774b-bcce-b302099a8057"]'),
        ],
    )
    def test_malformed_cursor_raises_validation_error(self, value: str) -> None:
        with pytest.raises(InvalidCursorError) as caught:
            decode_cursor(value)

        assert isinstance(caught.value, ValidationFailed)
        assert caught.value.errors[0].pointer == "/cursor"


class TestLimit:
    def test_default(self) -> None:
        assert validate_limit(None) == DEFAULT_LIMIT == 50

    @pytest.mark.parametrize("limit", [1, 50, MAX_LIMIT])
    def test_in_range(self, limit: int) -> None:
        assert validate_limit(limit) == limit

    @pytest.mark.parametrize("limit", [0, -1, MAX_LIMIT + 1, True, 1.5, "10"])
    def test_out_of_range(self, limit: object) -> None:
        with pytest.raises(ValidationFailed) as caught:
            validate_limit(limit)  # pyright: ignore[reportArgumentType]

        assert caught.value.errors[0].pointer == "/limit"


class TestPageParams:
    def test_defaults(self) -> None:
        params = PageParams()

        assert params.limit == DEFAULT_LIMIT
        assert params.decoded_cursor() is None

    def test_decodes_cursor(self) -> None:
        params = PageParams(cursor=encode_cursor(Cursor(AT, ROW_ID)), limit=10)

        assert params.decoded_cursor() == Cursor(AT, ROW_ID)

    @pytest.mark.parametrize("limit", [0, MAX_LIMIT + 1])
    def test_limit_bounds(self, limit: int) -> None:
        with pytest.raises(ValidationError):
            PageParams(limit=limit)


class TestBuildPage:
    rows: ClassVar = [(AT - timedelta(seconds=i), uuid.UUID(int=i)) for i in range(3)]

    def test_has_more_when_extra_row_fetched(self) -> None:
        page = build_page(self.rows, 2, to_item=lambda r: r[1].int, key=lambda r: Cursor(*r))

        assert page.data == [0, 1]
        assert page.has_more is True
        assert page.next_cursor is not None
        assert decode_cursor(page.next_cursor) == Cursor(*self.rows[1])

    def test_last_page(self) -> None:
        page = build_page(self.rows, 3, to_item=lambda r: r[1].int, key=lambda r: Cursor(*r))

        assert page == Page[int](data=[0, 1, 2], next_cursor=None, has_more=False)

    def test_empty(self) -> None:
        page = build_page([], 10, to_item=lambda r: r, key=lambda r: Cursor(*r))

        assert page.model_dump() == {"data": [], "next_cursor": None, "has_more": False}


class TestKeysetPredicate:
    def compile(self, order: str) -> str:
        statement = select(rows_table.c.id).where(
            keyset_predicate(
                rows_table.c.created_at,
                rows_table.c.id,
                Cursor(AT, ROW_ID),
                order,  # pyright: ignore[reportArgumentType]
            )
        )
        return str(statement.compile(dialect=postgresql.dialect()))

    def test_desc_uses_row_less_than(self) -> None:
        assert "WHERE (rows.created_at, rows.id) < (%(param_1)s" in self.compile("desc")

    def test_asc_uses_row_greater_than(self) -> None:
        assert "WHERE (rows.created_at, rows.id) > (%(param_1)s" in self.compile("asc")
