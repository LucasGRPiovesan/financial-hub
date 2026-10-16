"""Payments tuning from ``FH_PAYMENTS_*`` environment variables (W4.4; defaults suit dev and prod).

The sweeper schedule is registered only by the worker process, so test runs (which never start
the worker) have no sweeper unless a test creates the schedule itself.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class PaymentsSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FH_PAYMENTS_", env_file=".env", extra="ignore")

    # Periodic resolution of unknown creations/refunds and retry of unprocessed webhooks.
    sweeper_enabled: bool = True
    sweeper_interval_seconds: int = Field(default=60, ge=1, le=86_400)
    sweeper_batch_size: int = Field(default=100, ge=1, le=1000)
    # Inbound webhooks: a larger body is refused before it is read (Asaas sends a few KiB).
    webhook_max_body_bytes: int = Field(default=256 * 1024, ge=1024, le=1024 * 1024)
    # A failed row is retried by the sweeper once untouched this long, up to this many attempts.
    webhook_retry_after_seconds: int = Field(default=60, ge=1)
    webhook_max_attempts: int = Field(default=10, ge=1)
