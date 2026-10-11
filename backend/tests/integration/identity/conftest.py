from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from financial_hub.platform.db import create_engine
from financial_hub.platform.settings import get_settings
from tests.integration.identity.support import Api, app_database_url, running_api


@pytest.fixture
async def api() -> AsyncIterator[Api]:
    async with running_api() as running:
        yield running


@pytest.fixture
async def app_engine() -> AsyncIterator[AsyncEngine]:
    """Runtime role ``fh_app``: what the api uses."""
    engine = create_async_engine(app_database_url())
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def owner_engine() -> AsyncIterator[AsyncEngine]:
    """Owner login, only for setups the runtime role is not allowed to make (disabling)."""
    engine = create_engine(get_settings())
    try:
        yield engine
    finally:
        await engine.dispose()
