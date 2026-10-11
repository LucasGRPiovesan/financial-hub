from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from financial_hub.platform.db import create_engine
from financial_hub.platform.settings import get_settings
from tests.integration.identity.support import Api, running_api
from tests.integration.realtime.support import Rig


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


@pytest.fixture
async def rig(owner_engine: AsyncEngine) -> AsyncIterator[Rig]:
    async with Rig.open(owner_engine) as opened:
        yield opened
