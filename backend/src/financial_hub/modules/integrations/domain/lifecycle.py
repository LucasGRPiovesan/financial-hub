"""Lifecycle of a manifest version (ADR-0008).

::

    draft -> validated -> approved -> active -> superseded -> active (rollback) ...
      |          |           |
      +----------+-----------+-> rejected

- ``draft``: content can still be replaced. ``validated`` and later: content is immutable (also
  enforced by a trigger), so an approval always refers to exactly what was reviewed.
- ``approved``: needs ``integrations:approve``. **Four-eyes** (approver != author) is mandatory
  when the diff against the active version is money-sensitive (splits, prices, wallet/fee
  policy), and optional otherwise (``FH_INTEGRATIONS_FOUR_EYES_ALWAYS``).
- ``active``: at most one per project; activating another supersedes it. Rollback is activating
  a ``superseded`` version again: history (content, approvals, activations) is never rewritten.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final
from uuid import UUID

from financial_hub.shared.errors import Conflict, Forbidden


class ManifestStatus(StrEnum):
    DRAFT = "draft"
    VALIDATED = "validated"
    APPROVED = "approved"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


TRANSITIONS: Final[dict[ManifestStatus, frozenset[ManifestStatus]]] = {
    ManifestStatus.DRAFT: frozenset({ManifestStatus.VALIDATED, ManifestStatus.REJECTED}),
    ManifestStatus.VALIDATED: frozenset({ManifestStatus.APPROVED, ManifestStatus.REJECTED}),
    ManifestStatus.APPROVED: frozenset({ManifestStatus.ACTIVE, ManifestStatus.REJECTED}),
    ManifestStatus.ACTIVE: frozenset({ManifestStatus.SUPERSEDED}),
    ManifestStatus.SUPERSEDED: frozenset({ManifestStatus.ACTIVE}),
    ManifestStatus.REJECTED: frozenset(),
}


class IllegalTransition(Conflict):
    slug = "manifest-illegal-transition"
    title = "The manifest version cannot move to that state"


class FourEyesRequired(Forbidden):
    slug = "four-eyes-required"
    title = "A different operator must approve this manifest version."


def check_transition(current: ManifestStatus, target: ManifestStatus) -> None:
    if target not in TRANSITIONS[current]:
        raise IllegalTransition(f"A {current} manifest version cannot become {target}.")


def is_immutable(status: ManifestStatus) -> bool:
    return status is not ManifestStatus.DRAFT


def four_eyes_required(*, money_sensitive: bool, always: bool) -> bool:
    return always or money_sensitive


@dataclass(frozen=True, slots=True)
class ManifestVersion:
    id: UUID
    project_id: UUID
    version: int
    status: ManifestStatus
    content: dict[str, Any]
    content_hash: str
    created_by: str
    created_at: datetime
    updated_at: datetime
    validated_at: datetime | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None
    four_eyes: bool | None = None
    approval_base_version: int | None = None
    approval_diff: dict[str, Any] | None = None
    rejected_by: str | None = None
    rejected_at: datetime | None = None
    rejection_reason: str | None = None
    activated_at: datetime | None = None
    superseded_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Activation:
    id: UUID
    project_id: UUID
    version: int
    previous_version: int | None
    activated_by: str
    activated_at: datetime
    materialized: dict[str, Any]
