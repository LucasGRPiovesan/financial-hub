"""Canonical event registry (ADR-0005): the one list of event types the system can publish.

Each type is registered once per ``(name, version)`` with its pydantic payload model and a
description; docs and the webhook catalog are generated from :data:`registry`. ``publish()``
rejects anything not registered here. Business modules register their own types at import time
(``registry.register(...)`` in their ``application`` package); only ``system.test*`` types used by
the test suite are seeded here.
"""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from financial_hub.modules.events.domain import EVENT_TYPE_PATTERN, JsonObject
from financial_hub.shared.errors import FieldError, ValidationFailed


class UnregisteredEventType(ValidationFailed):
    """The event type (or version) is not in the canonical registry."""

    slug = "unregistered-event-type"
    title = "Unregistered event type"


class EventPayload(BaseModel):
    """Base for payload models: strict shape, immutable once built."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ScheduleDuePayload(EventPayload):
    """Payload of every event emitted by the scheduler (``<context>.<name>_due`` types)."""

    schedule: str
    due_at: datetime


@dataclass(frozen=True, slots=True)
class EventType[P: BaseModel]:
    name: str
    version: int
    payload: type[P]
    description: str

    def dump(self, data: BaseModel | Mapping[str, Any]) -> JsonObject:
        """Validate ``data`` against the payload model and return its JSON form."""
        if isinstance(data, BaseModel):
            if type(data) is not self.payload:
                raise ValidationFailed(
                    f"{self.name} v{self.version} expects {self.payload.__name__}, "
                    f"got {type(data).__name__}"
                )
            model: BaseModel = data
        else:
            model = self.parse(data)
        return model.model_dump(mode="json")

    def parse(self, data: Mapping[str, Any]) -> P:
        """Typed payload of a stored event (what handlers usually want)."""
        try:
            return self.payload.model_validate(dict(data))
        except ValidationError as exc:
            raise ValidationFailed(
                f"invalid payload for {self.name} v{self.version}",
                errors=[
                    FieldError(
                        pointer="/data/" + "/".join(str(part) for part in error["loc"]),
                        detail=error["msg"],
                        code=error["type"],
                    )
                    for error in exc.errors()
                ],
            ) from None


class EventRegistry:
    def __init__(self) -> None:
        self._types: dict[tuple[str, int], EventType[Any]] = {}

    def register[P: BaseModel](
        self, name: str, payload: type[P], *, description: str, version: int = 1
    ) -> EventType[P]:
        if EVENT_TYPE_PATTERN.fullmatch(name) is None:
            raise ValueError(f"invalid event type name {name!r}")
        if version < 1:
            raise ValueError("event type version must be >= 1")
        if not description.strip():
            raise ValueError(f"event type {name!r} needs a description")
        if (name, version) in self._types:
            raise ValueError(f"event type {name!r} v{version} is already registered")
        event_type = EventType(name, version, payload, description.strip())
        self._types[name, version] = event_type
        return event_type

    def get(self, name: str, version: int | None = None) -> EventType[Any]:
        """The registered type; ``version=None`` means the latest version of ``name``."""
        if version is None:
            versions = [v for (n, v) in self._types if n == name]
            if versions:
                return self._types[name, max(versions)]
        elif (found := self._types.get((name, version))) is not None:
            return found
        label = name if version is None else f"{name} v{version}"
        raise UnregisteredEventType(f"event type {label!r} is not registered")

    def __contains__(self, name: object) -> bool:
        return any(n == name for (n, _) in self._types)

    def __iter__(self) -> Iterator[EventType[Any]]:
        return iter(sorted(self._types.values(), key=lambda t: (t.name, t.version)))


registry = EventRegistry()
"""The canonical registry. Never instantiate another one outside tests."""


class SystemTestPayload(EventPayload):
    message: str


SYSTEM_TEST = registry.register(
    "system.test",
    SystemTestPayload,
    description="Test-only event used by the event log test suite and smoke checks.",
)
SYSTEM_TEST_HANDLED = registry.register(
    "system.test_handled",
    SystemTestPayload,
    description="Test-only effect written by test handlers (proves exactly-once effects).",
)
SYSTEM_TEST_SCHEDULE_DUE = registry.register(
    "system.test_schedule_due",
    ScheduleDuePayload,
    description="Test-only schedule event emitted by the scheduler.",
)
