"""Realtime fan-out for the SSE stream (ADR-0010).

One ``RealtimeBroker`` per process owns a single wake-up source (``LISTEN`` on the publisher's
channel, with plain polling as the floor) and one global ``(txid, id)`` cursor below the
visibility horizon (ADR-0005). Every wake-up reads the events after that cursor and hands each
one to the subscriptions of *its own project only*; a subscription is a bounded in-memory queue.

A connection (``RealtimeBroker.stream``) is: replay from ``Last-Event-ID`` out of the event log
(the log is the replay buffer), then live events from its queue. Replay and live may overlap;
the per-connection ``floor`` (last cursor sent) drops duplicates, so each event is delivered
exactly once and in ``(txid, id)`` order. Anything that cannot be delivered losslessly (invalid or
too old cursor, replay window exceeded, queue overflow) becomes a ``resync`` message: the client
refetches its state over the REST API and carries on with the live feed.
"""

import asyncio
from collections import deque
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import timedelta
from typing import Final, Literal, Protocol
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from financial_hub.modules.events.application.config import EventsSettings
from financial_hub.modules.events.domain import Event, EventCursor
from financial_hub.modules.events.infrastructure import listen, repository
from financial_hub.modules.events.infrastructure.listen import ListenWaker
from financial_hub.modules.events.infrastructure.pg import engine_dsn
from financial_hub.platform.db import create_session_factory
from financial_hub.shared.clock import Clock, SystemClock
from financial_hub.shared.pagination import InvalidCursorError

logger = structlog.get_logger()

_NO_FILTERS: Final[list[str]] = []


class Waker(Protocol):
    async def wait(self, seconds: float) -> bool:
        """Block until something may have happened or ``seconds`` passed."""
        ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class RealtimeConfig:
    heartbeat_seconds: float = 15.0
    queue_size: int = 256
    poll_interval_seconds: float = 2.0
    batch_size: int = 500
    replay_max_events: int = 1000
    replay_max_age: timedelta = timedelta(hours=24)

    @classmethod
    def from_settings(cls, settings: EventsSettings) -> RealtimeConfig:
        return cls(
            heartbeat_seconds=settings.stream_heartbeat_seconds,
            queue_size=settings.stream_queue_size,
            poll_interval_seconds=settings.stream_poll_interval_seconds,
            batch_size=settings.stream_batch_size,
            replay_max_events=settings.stream_replay_max_events,
            replay_max_age=timedelta(seconds=settings.stream_replay_max_age_seconds),
        )


@dataclass(frozen=True, slots=True)
class StreamMessage:
    kind: Literal["event", "resync", "heartbeat"]
    event: Event | None = None


RESYNC: Final = StreamMessage("resync")
HEARTBEAT: Final = StreamMessage("heartbeat")


class ResyncMarker:
    """Queue marker: the backlog was dropped."""


RESYNC_MARK: Final = ResyncMarker()


class Subscription:
    """A bounded per-connection queue of one project's events.

    ``offer`` never blocks the broker: when the queue is full the backlog is dropped and a single
    resync marker takes its place (further offers are discarded until the consumer has seen it,
    because the client will refetch state after the marker anyway).
    """

    def __init__(self, project_id: UUID, limit: int) -> None:
        self.project_id = project_id
        self._limit = limit
        self._items: deque[Event] = deque()
        self._resync = False
        self._ready = asyncio.Event()

    def offer(self, event: Event) -> None:
        if self._resync:
            return
        if len(self._items) >= self._limit:
            self._items.clear()
            self._resync = True
        else:
            self._items.append(event)
        self._ready.set()

    async def get(self, timeout: float) -> Event | ResyncMarker | None:  # noqa: ASYNC109 - the heartbeat period
        """Next item; ``None`` when ``timeout`` elapsed with nothing to deliver."""
        while True:
            if self._resync:
                self._resync = False
                return RESYNC_MARK
            if self._items:
                return self._items.popleft()
            self._ready.clear()
            try:
                await asyncio.wait_for(self._ready.wait(), timeout)
            except TimeoutError:
                return None


@dataclass(frozen=True, slots=True)
class _Replay:
    events: list[Event]
    floor: EventCursor | None
    resync: bool


class RealtimeBroker:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        waker: Waker,
        *,
        config: RealtimeConfig | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._waker = waker
        self.config = config or RealtimeConfig()
        self._clock = clock or SystemClock()
        self._subscriptions: dict[UUID, set[Subscription]] = {}
        self._cursor: EventCursor | None = None
        self._task: asyncio.Task[None] | None = None

    # --- subscriptions ---------------------------------------------------------------------

    @property
    def cursor(self) -> EventCursor | None:
        """How far the broker has read (``None``: nothing yet)."""
        return self._cursor

    @property
    def subscriber_count(self) -> int:
        return sum(len(subs) for subs in self._subscriptions.values())

    def subscribe(self, project_id: UUID) -> Subscription:
        subscription = Subscription(project_id, self.config.queue_size)
        self._subscriptions.setdefault(project_id, set()).add(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        subscriptions = self._subscriptions.get(subscription.project_id)
        if subscriptions is None:
            return
        subscriptions.discard(subscription)
        if not subscriptions:
            del self._subscriptions[subscription.project_id]

    # --- lifecycle -------------------------------------------------------------------------

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run(), name="realtime-broker")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._waker.close()

    async def run(self) -> None:
        """Pump until cancelled; failures are logged and retried at the poll interval."""
        interval = self.config.poll_interval_seconds
        while not await self.anchor():  # noqa: ASYNC110 - retry until the database is up
            await asyncio.sleep(interval)
        while True:
            try:
                await self.pump()
            except Exception as exc:
                logger.warning("events.stream_pump_failed", error=f"{type(exc).__name__}: {exc}")
            await self._waker.wait(interval)

    async def anchor(self) -> bool:
        """Start live at the head of the log: history is the replay path's business."""
        try:
            async with self._session_factory() as session:
                self._cursor = await listen.latest_cursor(session)
        except Exception as exc:
            logger.warning("events.stream_anchor_failed", error=f"{type(exc).__name__}: {exc}")
            return False
        return True

    async def pump(self) -> int:
        """Read everything after the broker cursor and dispatch it; returns how many events."""
        total = 0
        while True:
            async with self._session_factory() as session:
                events = await listen.events_after(session, self._cursor, self.config.batch_size)
            for event in events:
                for subscription in tuple(self._subscriptions.get(event.project_id, ())):
                    subscription.offer(event)
                self._cursor = event.cursor
            total += len(events)
            if len(events) < self.config.batch_size:
                return total

    # --- one connection --------------------------------------------------------------------

    async def stream(
        self, project_id: UUID, last_event_id: str | None = None
    ) -> AsyncGenerator[StreamMessage]:
        subscription = self.subscribe(project_id)  # before replay: nothing falls in between
        try:
            floor: EventCursor | None = None
            if last_event_id:
                replay = await self._replay(project_id, last_event_id)
                if replay.resync:
                    yield RESYNC
                floor = replay.floor
                for event in replay.events:
                    floor = event.cursor
                    yield StreamMessage("event", event)
            while True:
                item = await subscription.get(self.config.heartbeat_seconds)
                if item is None:
                    yield HEARTBEAT
                elif isinstance(item, ResyncMarker):
                    yield RESYNC
                elif floor is None or item.cursor > floor:
                    floor = item.cursor
                    yield StreamMessage("event", item)
        finally:
            self.unsubscribe(subscription)

    async def _replay(self, project_id: UUID, last_event_id: str) -> _Replay:
        """Events after ``last_event_id`` inside the window, or a resync when it cannot be served.

        The id must name an event of *this* project (also keeps probing other projects' cursors
        from learning anything): unknown, foreign, malformed, older than ``replay_max_age`` or
        more than ``replay_max_events`` behind all mean resync.
        """
        try:
            cursor = EventCursor.decode(last_event_id)
        except InvalidCursorError:
            return _Replay([], None, resync=True)
        config = self.config
        async with self._session_factory() as session:
            anchor = await repository.get_event(session, project_id, cursor.id)
            if anchor is None or anchor.txid != cursor.txid:
                return _Replay([], None, resync=True)
            if self._clock.now() - anchor.occurred_at > config.replay_max_age:
                return _Replay([], None, resync=True)
            events = await repository.list_events_after(
                session,
                project_id=project_id,
                cursor=cursor,
                types=_NO_FILTERS,
                subject=None,
                limit=config.replay_max_events + 1,
            )
        if len(events) > config.replay_max_events:
            return _Replay([], None, resync=True)
        return _Replay(events, cursor, resync=False)


def build_broker(
    engine: AsyncEngine, *, settings: EventsSettings | None = None, clock: Clock | None = None
) -> RealtimeBroker:
    """The process's broker on ``engine``'s database (the API starts it in its lifespan)."""
    settings = settings or EventsSettings()
    return RealtimeBroker(
        create_session_factory(engine),
        ListenWaker(
            engine_dsn(engine),
            backoff_base_seconds=settings.stream_listen_backoff_base_seconds,
            backoff_cap_seconds=settings.stream_listen_backoff_cap_seconds,
        ),
        config=RealtimeConfig.from_settings(settings),
        clock=clock,
    )
