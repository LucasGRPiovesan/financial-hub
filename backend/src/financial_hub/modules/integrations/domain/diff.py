"""Structured diff between two manifest versions; flags the money-sensitive sections.

Sections are compared in a fixed order. Keyed lists are compared item by item (business events by
``name``, metrics and prices by ``code``, splits by ``payee``, webhooks by ``url``); every other
section as a whole. A change to ``splits``, ``prices`` or ``wallet`` (the fee policy) is
*money-sensitive*: approving it always needs a second operator (four-eyes, ADR-0008).

Without a base version (nothing active yet) the diff is taken against an empty manifest, so a
first manifest with splits or prices is money-sensitive too.
"""

from dataclasses import dataclass
from typing import Any, Final, Literal

from financial_hub.modules.integrations.domain.manifest import ManifestV1, WalletPolicy

KEYED_SECTIONS: Final = {
    "business_events": "name",
    "metrics": "code",
    "prices": "code",
    "splits": "payee",
    "webhooks": "url",
}
WHOLE_SECTIONS: Final = ("schema_version", "description", "product", "customers", "wallet")
SECTION_ORDER: Final = (
    "schema_version",
    "description",
    "product",
    "customers",
    "business_events",
    "metrics",
    "prices",
    "splits",
    "wallet",
    "webhooks",
)
MONEY_SENSITIVE: Final = frozenset({"splits", "prices", "wallet"})

type ChangeKind = Literal["added", "removed", "changed"]


@dataclass(frozen=True, slots=True)
class Change:
    section: str
    key: str | None  # the item key in a keyed section, ``None`` for a whole section
    kind: ChangeKind
    before: Any
    after: Any

    @property
    def money_sensitive(self) -> bool:
        return self.section in MONEY_SENSITIVE

    def as_json(self) -> dict[str, Any]:
        return {
            "section": self.section,
            "key": self.key,
            "change": self.kind,
            "money_sensitive": self.money_sensitive,
            "before": self.before,
            "after": self.after,
        }


@dataclass(frozen=True, slots=True)
class ManifestDiff:
    base_version: int | None
    changes: tuple[Change, ...]

    @property
    def sensitive_sections(self) -> tuple[str, ...]:
        found = {change.section for change in self.changes if change.money_sensitive}
        return tuple(section for section in SECTION_ORDER if section in found)

    @property
    def money_sensitive(self) -> bool:
        return bool(self.sensitive_sections)

    def as_json(self) -> dict[str, Any]:
        return {
            "base_version": self.base_version,
            "money_sensitive": self.money_sensitive,
            "sensitive_sections": list(self.sensitive_sections),
            "changes": [change.as_json() for change in self.changes],
        }


def _comparable(manifest: ManifestV1 | None) -> dict[str, Any]:
    if manifest is None:
        empty: dict[str, Any] = dict.fromkeys(SECTION_ORDER)
        empty.update({section: [] for section in KEYED_SECTIONS})
        empty["wallet"] = WalletPolicy().model_dump(mode="json", by_alias=True)
        return empty
    return manifest.model_dump(mode="json", by_alias=True)


def diff_manifests(
    before: ManifestV1 | None, after: ManifestV1, *, base_version: int | None = None
) -> ManifestDiff:
    """What ``after`` changes relative to ``before`` (``None``: nothing active)."""
    old, new = _comparable(before), _comparable(after)
    changes: list[Change] = []
    for section in SECTION_ORDER:
        if section in KEYED_SECTIONS:
            changes += _keyed(section, KEYED_SECTIONS[section], old[section], new[section])
        elif old[section] != new[section]:
            kind: ChangeKind = (
                "added"
                if old[section] is None
                else "removed"
                if new[section] is None
                else "changed"
            )
            changes.append(Change(section, None, kind, old[section], new[section]))
    return ManifestDiff(base_version=base_version, changes=tuple(changes))


def _keyed(
    section: str, key: str, old: list[dict[str, Any]], new: list[dict[str, Any]]
) -> list[Change]:
    before = {item[key]: item for item in old}
    after = {item[key]: item for item in new}
    changes: list[Change] = []
    for name in sorted(before.keys() | after.keys()):
        was, now = before.get(name), after.get(name)
        if was == now:
            continue
        kind: ChangeKind = "added" if was is None else "removed" if now is None else "changed"
        changes.append(Change(section, name, kind, was, now))
    return changes
