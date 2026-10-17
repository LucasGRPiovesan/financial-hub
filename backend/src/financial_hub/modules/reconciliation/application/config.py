"""Reconciliation tuning from ``FH_RECONCILIATION_*`` variables (docs/RECONCILIATION.md).

The daily schedule is registered only by the worker process and is **off** by default: a run
reads the provider (``GET /payments``, ``GET /finance/balance``), so it is opted into per
environment. Tests never start the worker, so they have no schedule unless a test creates it.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ReconciliationSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FH_RECONCILIATION_", env_file=".env", extra="ignore"
    )

    # Each due tick reconciles the previous day (America/Sao_Paulo) of every account.
    scheduler_enabled: bool = False
    scheduler_interval_seconds: int = Field(default=86_400, ge=60, le=7 * 86_400)
