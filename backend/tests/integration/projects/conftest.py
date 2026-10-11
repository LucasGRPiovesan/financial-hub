from collections.abc import AsyncIterator

import pytest

from tests.integration.identity.support import Api, running_api


@pytest.fixture
async def api() -> AsyncIterator[Api]:
    async with running_api() as running:
        yield running
