"""Internal handler contract (ADR-0004/0005).

A handler is ``async (event, uow) -> None``. It runs inside the dispatcher's transaction, which
already holds the delivery's lease (compare-and-set on ``lease_token``) and has written the
``inbox`` row for ``(handler, event_id)``: every write made through ``uow`` commits together with
the inbox row and the delivery's completion, or not at all. A redelivered event whose inbox row
exists is acknowledged without calling the handler again. Raising schedules a retry with
backoff (and ``dead`` after the last attempt); never swallow errors to "succeed". External calls
must not happen inside the handler: publish an event or register an ``after_commit`` hook.
"""

from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass

from financial_hub.modules.events.domain import Event, is_valid_type_filter, require_name
from financial_hub.platform.db import UnitOfWork

type Handler = Callable[[Event, UnitOfWork], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class HandlerSpec:
    name: str
    handler: Handler
    # Filters of the global subscription the worker ensures at start-up; empty means the
    # subscriptions are managed explicitly (e.g. per project) through the events service.
    event_types: tuple[str, ...] = ()


class HandlerRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, HandlerSpec] = {}

    def register(self, name: str, handler: Handler, *, event_types: Sequence[str] = ()) -> None:
        require_name("handler", name)
        if name in self._specs:
            raise ValueError(f"handler {name!r} is already registered")
        for pattern in event_types:
            if not is_valid_type_filter(pattern):
                raise ValueError(f"invalid event type filter {pattern!r}")
        self._specs[name] = HandlerSpec(name, handler, tuple(event_types))

    def get(self, name: str) -> Handler | None:
        spec = self._specs.get(name)
        return None if spec is None else spec.handler

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._specs))

    def __iter__(self) -> Iterator[HandlerSpec]:
        return iter(sorted(self._specs.values(), key=lambda spec: spec.name))

    def __len__(self) -> int:
        return len(self._specs)


handlers = HandlerRegistry()
"""Handlers the worker runs. Business modules register theirs at import time."""
