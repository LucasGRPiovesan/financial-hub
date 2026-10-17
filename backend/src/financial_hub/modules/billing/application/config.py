"""Billing tuning from ``FH_BILLING_*`` environment variables (docs/DOMAIN.md "Billing").

The period-close schedule is registered only by the worker process. It is on by default: closing
a period only reads the hub's own tables and books R1 (the payment request is sent after commit).
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class BillingSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FH_BILLING_", env_file=".env", extra="ignore")

    close_enabled: bool = True
    close_interval_seconds: int = Field(default=300, ge=60, le=86_400)
    # Subscriptions closed per tick (each period in its own transaction); the rest wait a tick.
    close_batch_size: int = Field(default=100, ge=1, le=1_000)
