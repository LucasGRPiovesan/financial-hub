"""Cursor pagination: opaque keyset cursors on ``(created_at, id)`` (ADR-0001)."""

import base64
import binascii
import json
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal

from pydantic import BaseModel, Field
from sqlalchemy import ColumnElement, tuple_

from financial_hub.shared.errors import FieldError, ValidationFailed

DEFAULT_LIMIT: Final = 50
MAX_LIMIT: Final = 200

type SortOrder = Literal["asc", "desc"]


class InvalidCursorError(ValidationFailed):
    def __init__(self) -> None:
        super().__init__(
            "The cursor is malformed or expired.",
            errors=[FieldError(pointer="/cursor", detail="invalid cursor", code="invalid_cursor")],
        )


@dataclass(frozen=True, slots=True)
class Cursor:
    """Position after the last returned row. ``created_at`` must be timezone-aware."""

    created_at: datetime
    id: uuid.UUID

    def __post_init__(self) -> None:
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("cursor created_at must be timezone-aware")


def encode_cursor(cursor: Cursor) -> str:
    """base64url (unpadded) JSON ``[created_at_iso, uuid]``. Opaque to clients."""
    payload = json.dumps([cursor.created_at.isoformat(), str(cursor.id)], separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).rstrip(b"=").decode("ascii")


def decode_cursor(value: str) -> Cursor:
    """Inverse of :func:`encode_cursor`; any malformed input raises :class:`InvalidCursorError`."""
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        decoded: object = json.loads(raw)
        if not isinstance(decoded, list) or len(decoded) != 2:  # pyright: ignore[reportUnknownArgumentType]
            raise InvalidCursorError
        created_at, row_id = decoded  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(created_at, str) or not isinstance(row_id, str):
            raise InvalidCursorError
        return Cursor(created_at=datetime.fromisoformat(created_at), id=uuid.UUID(row_id))
    except binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError:
        raise InvalidCursorError from None


def validate_limit(limit: int | None) -> int:
    """``None`` -> default; otherwise must be an int in ``1..MAX_LIMIT``."""
    if limit is None:
        return DEFAULT_LIMIT
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise ValidationFailed(
            f"limit must be between 1 and {MAX_LIMIT}",
            errors=[FieldError(pointer="/limit", detail=f"must be 1..{MAX_LIMIT}", code="range")],
        )
    return limit


class PageParams(BaseModel):
    """Query parameters of a list endpoint (use as ``Annotated[PageParams, Query()]``)."""

    cursor: str | None = None
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)

    def decoded_cursor(self) -> Cursor | None:
        return None if self.cursor is None else decode_cursor(self.cursor)


class Page[T](BaseModel):
    data: list[T]
    next_cursor: str | None
    has_more: bool


def keyset_predicate(
    created_at_column: ColumnElement[datetime],
    id_column: ColumnElement[uuid.UUID],
    cursor: Cursor,
    order: SortOrder = "desc",
) -> ColumnElement[bool]:
    """``WHERE (created_at, id) </> (cursor.created_at, cursor.id)`` (index-friendly row compare).

    Pair it with ``ORDER BY created_at <order>, id <order>`` and ``LIMIT limit + 1``.
    """
    row = tuple_(created_at_column, id_column)
    position = tuple_(cursor.created_at, cursor.id)
    return row < position if order == "desc" else row > position


def build_page[T](
    rows: Sequence[Any], limit: int, to_item: Callable[[Any], T], key: Callable[[Any], Cursor]
) -> Page[T]:
    """Turn ``limit + 1`` fetched rows into a page; the extra row only signals ``has_more``."""
    has_more = len(rows) > limit
    visible = rows[:limit]
    next_cursor = encode_cursor(key(visible[-1])) if has_more and visible else None
    return Page[T](
        data=[to_item(row) for row in visible], next_cursor=next_cursor, has_more=has_more
    )
