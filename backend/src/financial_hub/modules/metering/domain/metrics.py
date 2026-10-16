"""Billable metrics (pure): how the usage events of a project are aggregated."""

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID

CODE_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
FIELD_PATTERN: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
NAME_MAX_LENGTH: Final = 100
DESCRIPTION_MAX_LENGTH: Final = 500


class Aggregation(StrEnum):
    """``sum`` and ``max`` read a decimal property, ``unique_count`` the distinct values of a
    scalar property, ``count`` needs no property (it counts events)."""

    SUM = "sum"
    COUNT = "count"
    MAX = "max"
    UNIQUE_COUNT = "unique_count"


class MetricStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


@dataclass(frozen=True, slots=True)
class BillableMetric:
    id: UUID
    project_id: UUID
    code: str
    name: str
    description: str | None
    aggregation: Aggregation
    field: str | None
    status: MetricStatus
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None

    @property
    def is_active(self) -> bool:
        return self.status is MetricStatus.ACTIVE


def validate_code(value: str) -> str:
    if CODE_PATTERN.fullmatch(value) is None:
        raise ValueError(
            "code must be 1-63 characters: a lowercase letter, then lowercase letters, digits or _"
        )
    return value


def validate_name(value: str) -> str:
    cleaned = value.strip()
    if not 1 <= len(cleaned) <= NAME_MAX_LENGTH:
        raise ValueError(f"name must have 1..{NAME_MAX_LENGTH} characters")
    return cleaned


def validate_description(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not 1 <= len(cleaned) <= DESCRIPTION_MAX_LENGTH:
        raise ValueError(f"description must have 1..{DESCRIPTION_MAX_LENGTH} characters")
    return cleaned


def validate_definition(aggregation: str, field: str | None) -> tuple[Aggregation, str | None]:
    """The aggregation and the property it reads: ``field`` is required unless it is ``count``."""
    try:
        parsed = Aggregation(aggregation)
    except ValueError:
        allowed = ", ".join(a.value for a in Aggregation)
        raise ValueError(f"aggregation must be one of: {allowed}") from None
    if parsed is Aggregation.COUNT:
        if field is not None:
            raise ValueError("a count metric counts events and takes no field")
        return parsed, None
    if field is None:
        raise ValueError(f"a {parsed.value} metric needs the event property it reads (field)")
    if FIELD_PATTERN.fullmatch(field) is None:
        raise ValueError("field must be 1-64 characters: a letter or _, then letters, digits or _")
    return parsed, field
