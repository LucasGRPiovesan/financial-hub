"""Fixtures shared with the identity suite (real Postgres: runtime role and owner engines)."""

from tests.integration.identity.conftest import api, app_engine, owner_engine

__all__ = ["api", "app_engine", "owner_engine"]
