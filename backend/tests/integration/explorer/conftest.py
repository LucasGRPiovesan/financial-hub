from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from financial_hub.platform.db import create_engine
from financial_hub.platform.settings import get_settings
from tests.integration.identity.support import Api, running_api


@pytest.fixture
async def api() -> AsyncIterator[Api]:
    async with running_api() as running:
        yield running


@pytest.fixture
async def owner_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_engine(get_settings())
    try:
        yield engine
    finally:
        await engine.dispose()
