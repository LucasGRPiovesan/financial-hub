"""Fixtures of the ledger API suite: the identity ``api`` app plus seeded projects (Postgres)."""

from collections.abc import AsyncIterator
from contextlib import aclosing

import httpx
import pytest

from tests.integration.identity.conftest import api, app_engine, owner_engine
from tests.integration.identity.support import Api, logged_in
from tests.integration.ledger.api.seed import Seeded, seed_project
from tests.integration.projects.helpers import create_project

__all__ = ["api", "app_engine", "owner_engine"]


@pytest.fixture
async def seeded(api: Api) -> Seeded:
    """Project A (owner logged in) with a mixed recipe flow already applied."""
    owner = await api.operator()
    project = await create_project(api, owner)
    return await seed_project(api, project["id"], owner)


@pytest.fixture
async def other(api: Api) -> Seeded:
    """Project B of another owner, seeded the same way (for isolation checks)."""
    owner = await api.operator()
    project = await create_project(api, owner)
    return await seed_project(api, project["id"], owner)


@pytest.fixture
async def client(api: Api, seeded: Seeded) -> AsyncIterator[httpx.AsyncClient]:
    """The owner of project A, logged in."""
    session, _ = await logged_in(api, seeded.owner)
    async with aclosing(session):
        yield session


@pytest.fixture
async def other_client(api: Api, other: Seeded) -> AsyncIterator[httpx.AsyncClient]:
    """The owner of project B, logged in."""
    session, _ = await logged_in(api, other.owner)
    async with aclosing(session):
        yield session
