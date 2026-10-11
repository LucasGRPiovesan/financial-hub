from collections.abc import AsyncIterator
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from financial_hub.platform.db import create_engine, create_session_factory
from financial_hub.platform.settings import get_settings
from financial_hub.shared.ids import new_uuid
from tests.integration.ledger.support import Ledger, make_app_engine


@pytest.fixture
async def app_engine() -> AsyncIterator[AsyncEngine]:
    """Runtime role ``fh_app``: what the api and worker use."""
    engine = make_app_engine()
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def owner_engine() -> AsyncIterator[AsyncEngine]:
    """Migration/owner login: proves the triggers hold even without the privilege barrier."""
    engine = create_engine(get_settings())
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def app_factory(app_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(app_engine)


@pytest.fixture
def ledger(app_factory: async_sessionmaker[AsyncSession]) -> Ledger:
    return Ledger(app_factory)


@pytest.fixture
def project_id() -> UUID:
    return new_uuid()
