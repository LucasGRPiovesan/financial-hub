"""Projects domain (pure). Other modules use ``financial_hub.modules.projects.application.api``."""

from financial_hub.modules.projects.domain.model import (
    NAME_MAX_LENGTH,
    SLUG_PATTERN,
    Project,
    ProjectStatus,
    validate_name,
    validate_slug,
)

__all__ = [
    "NAME_MAX_LENGTH",
    "SLUG_PATTERN",
    "Project",
    "ProjectStatus",
    "validate_name",
    "validate_slug",
]
