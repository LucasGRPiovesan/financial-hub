"""Projects: the tenant boundary (ADR-0011). API keys and provider accounts live beside it."""

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID

SLUG_PATTERN: Final = re.compile(r"[a-z][a-z0-9-]{1,48}[a-z0-9]")
NAME_MAX_LENGTH: Final = 100


class ProjectStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


@dataclass(frozen=True, slots=True)
class Project:
    id: UUID
    slug: str
    name: str
    status: ProjectStatus
    created_at: datetime


def validate_slug(slug: str) -> str:
    """3..50 chars of ``a-z0-9-``, starting with a letter and not ending with ``-``."""
    if SLUG_PATTERN.fullmatch(slug) is None:
        raise ValueError(
            "slug must be 3 to 50 lowercase letters, digits or '-', start with a letter "
            "and not end with '-'"
        )
    return slug


def validate_name(name: str) -> str:
    value = name.strip()
    if not 1 <= len(value) <= NAME_MAX_LENGTH:
        raise ValueError(f"name must have 1 to {NAME_MAX_LENGTH} characters")
    return value
