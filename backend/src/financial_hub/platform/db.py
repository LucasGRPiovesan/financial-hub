"""Database access: async engine (psycopg 3), sessions, Unit of Work, declarative base."""

from collections.abc import Awaitable, Callable
from datetime import datetime
from types import TracebackType
from typing import Any, ClassVar, Self

import structlog
from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.ext.asyncio import (
    AsyncAttrs,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from financial_hub.platform.settings import Settings

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)

logger = structlog.get_logger()

type AfterCommitHook = Callable[[], Awaitable[None]]


class Base(AsyncAttrs, DeclarativeBase):
    metadata = metadata
    # Every datetime column is timestamptz; naive datetimes never reach the database.
    type_annotation_map: ClassVar[dict[Any, Any]] = {datetime: DateTime(timezone=True)}


class TimestampMixin:
    """``created_at``/``updated_at`` set by the database (``now()`` = transaction start)."""

    # eager_defaults: fetch server-generated values with RETURNING (no lazy IO under asyncio).
    __mapper_args__: ClassVar[dict[str, Any]] = {"eager_defaults": True}

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(settings.database_url.get_secret_value(), pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


class UnitOfWork:
    """One database transaction per command (ARCHITECTURE 4.2).

    Commits when the block exits normally, rolls back on any exception (including cancellation).
    ``after_commit`` hooks run only after a successful commit; a failing hook is logged and does
    not affect the (already committed) transaction or the other hooks. Never call external
    providers inside the block: register a hook or use the event log instead.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None
        self.after_commit: list[AfterCommitHook] = []

    @property
    def session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("UnitOfWork is not active; use 'async with uow:'")
        return self._session

    async def __aenter__(self) -> Self:
        if self._session is not None:
            raise RuntimeError("UnitOfWork is already active (nesting is not supported)")
        self.after_commit = []
        session = self._session_factory()
        try:
            await session.begin()
        except BaseException:
            await session.close()
            raise
        self._session = session
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        session = self.session
        hooks, self.after_commit = self.after_commit, []
        try:
            if exc is None:
                await session.commit()
            else:
                await session.rollback()
        finally:
            await session.close()
            self._session = None
        if exc is None:
            await self._run_after_commit(hooks)

    @staticmethod
    async def _run_after_commit(hooks: list[AfterCommitHook]) -> None:
        for hook in hooks:
            try:
                await hook()
            except Exception:
                logger.exception("uow.after_commit_failed", hook=getattr(hook, "__name__", "?"))
