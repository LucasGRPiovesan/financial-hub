import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from sqlalchemy import String, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from financial_hub.platform.db import (
    Base,
    TimestampMixin,
    UnitOfWork,
    create_engine,
    create_session_factory,
)
from financial_hub.platform.settings import get_settings


class _TestBase(DeclarativeBase):
    """Separate registry so the probe table never leaks into the app metadata/migrations."""

    type_annotation_map = Base.type_annotation_map


class Probe(TimestampMixin, _TestBase):
    __tablename__ = "uow_probe"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(50))


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    engine = create_engine(get_settings())
    async with engine.begin() as connection:
        # A real table (not TEMPORARY: those are per pooled connection), dropped again below.
        await connection.run_sync(Probe.metadata.create_all)
    try:
        yield engine
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Probe.metadata.drop_all)
        await engine.dispose()


@pytest.fixture
def factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


async def count(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as session:
        return len((await session.scalars(select(Probe))).all())


async def test_commit_persists_and_runs_hooks(factory: async_sessionmaker[AsyncSession]) -> None:
    calls: list[str] = []

    async def hook() -> None:
        calls.append("after")

    uow = UnitOfWork(factory)
    async with uow:
        uow.session.add(Probe(name="committed"))
        uow.after_commit.append(hook)
        assert calls == []

    assert calls == ["after"]
    assert await count(factory) == 1


async def test_exception_rolls_back_and_skips_hooks(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    calls: list[str] = []

    async def hook() -> None:
        calls.append("after")

    uow = UnitOfWork(factory)
    with pytest.raises(RuntimeError, match="boom"):
        async with uow:
            uow.session.add(Probe(name="rolled back"))
            await uow.session.flush()
            uow.after_commit.append(hook)
            raise RuntimeError("boom")

    assert calls == []
    assert await count(factory) == 0


async def test_failing_hook_does_not_stop_others(factory: async_sessionmaker[AsyncSession]) -> None:
    calls: list[str] = []

    async def failing() -> None:
        raise ValueError("hook failed")

    async def succeeding() -> None:
        calls.append("ok")

    uow = UnitOfWork(factory)
    async with uow:
        uow.session.add(Probe(name="kept"))
        uow.after_commit.extend([failing, succeeding])

    assert calls == ["ok"]
    assert await count(factory) == 1


async def test_failed_commit_raises_and_skips_hooks(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    calls: list[str] = []
    row_id = uuid.uuid4()

    async def hook() -> None:
        calls.append("after")

    async with UnitOfWork(factory) as uow:
        uow.session.add(Probe(id=row_id, name="first"))

    uow = UnitOfWork(factory)
    with pytest.raises(IntegrityError, match="uow_probe"):
        async with uow:
            uow.session.add(Probe(id=row_id, name="duplicate"))
            uow.after_commit.append(hook)

    assert calls == []
    assert await count(factory) == 1
    with pytest.raises(RuntimeError, match="not active"):
        _ = uow.session


async def test_session_outside_block_and_nesting_raise(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    uow = UnitOfWork(factory)
    with pytest.raises(RuntimeError, match="not active"):
        _ = uow.session

    async with uow:
        with pytest.raises(RuntimeError, match="already active"):
            await uow.__aenter__()

    # Reusable after the block ends.
    async with uow:
        await uow.session.execute(text("SELECT 1"))


async def test_timestamp_mixin_uses_database_defaults(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with UnitOfWork(factory) as uow:
        probe = Probe(name="stamped")
        uow.session.add(probe)
        await uow.session.flush()
        # eager_defaults: server values are loaded by RETURNING, no lazy load under asyncio.
        created_at, updated_at = probe.created_at, probe.updated_at

    assert created_at.tzinfo is not None
    assert created_at == updated_at

    async with UnitOfWork(factory) as uow:
        await uow.session.execute(text("SELECT pg_sleep(0.01)"))
        stored = await uow.session.get_one(Probe, probe.id)
        stored.name = "renamed"
        await uow.session.flush()
        assert stored.updated_at > created_at
        assert stored.created_at == created_at
        assert stored.updated_at - created_at < timedelta(minutes=1)
