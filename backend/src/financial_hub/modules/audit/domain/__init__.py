"""Audit domain: who did what to which target, and the redaction rules for what is stored.

Pure types, no I/O. A record is immutable once written (database triggers, migration 0005); the
``diff`` it carries holds before/after values of non-secret fields only.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, cast

REDACTED: Final = "[redacted]"
USER_AGENT_MAX_LENGTH: Final = 256
# Defence in depth: callers already leave secrets out; any key that still names one is masked.
_SENSITIVE_KEY: Final = re.compile(
    r"secret|hash|cipher|password|passwd|token|credential|nonce|plaintext|document|cpf|cnpj"
    r"|api_?key|^key$",
    re.IGNORECASE,
)


class ActorType(StrEnum):
    OPERATOR = "operator"
    API_KEY = "api_key"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class Actor:
    type: ActorType
    id: str | None = None

    @classmethod
    def system(cls) -> Actor:
        return cls(ActorType.SYSTEM)

    @classmethod
    def of_principal(cls, kind: str, subject_id: str) -> Actor:
        """The actor behind an authenticated principal (``operator`` or ``api_key``)."""
        try:
            return cls(ActorType(kind), subject_id)
        except ValueError:  # an unknown principal kind is audited as the system, never dropped
            return cls(ActorType.SYSTEM, subject_id)


@dataclass(frozen=True, slots=True)
class AuditTarget:
    type: str
    id: str | None = None


def redact(value: Any, *, key: str | None = None) -> Any:
    """``value`` with every field whose key names a secret replaced by ``"[redacted]"``."""
    if key is not None and _SENSITIVE_KEY.search(key):
        return REDACTED
    if isinstance(value, Mapping):
        mapping = cast("Mapping[Any, Any]", value)
        return {str(k): redact(v, key=str(k)) for k, v in mapping.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact(item) for item in value]  # pyright: ignore[reportUnknownVariableType]
    return value


def changed_fields(
    before: Mapping[str, Any] | None, after: Mapping[str, Any] | None
) -> dict[str, Any]:
    """``{"before": ..., "after": ...}`` restricted to the fields that differ.

    ``before=None`` (creation) records every ``after`` field; ``after=None`` (removal) every
    ``before`` field. Redaction happens when the record is written.
    """
    if before is None:
        return {"after": dict(after or {})}
    if after is None:
        return {"before": dict(before)}
    keys = [key for key in after if key not in before or before[key] != after[key]]
    return {
        "before": {key: before.get(key) for key in keys},
        "after": {key: after[key] for key in keys},
    }
