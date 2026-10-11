"""Event log tuning from ``FH_EVENTS_*`` environment variables (defaults suit dev and prod)."""

from datetime import timedelta

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from financial_hub.modules.events.domain import RetryPolicy


class EventsSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FH_EVENTS_", env_file=".env", extra="ignore")

    batch_size: int = Field(default=50, ge=1, le=1000)
    lease_seconds: float = Field(default=60.0, gt=0)
    # Handlers must finish well inside the lease; a timeout counts as a failed attempt.
    handler_timeout_seconds: float = Field(default=30.0, gt=0)
    poll_interval_seconds: float = Field(default=2.0, gt=0)
    stats_interval_seconds: float = Field(default=15.0, gt=0)
    retry_base_seconds: float = Field(default=1.0, gt=0)
    retry_cap_seconds: float = Field(default=3600.0, gt=0)
    max_attempts: int = Field(default=10, ge=1)
    scheduler_tick_seconds: float = Field(default=5.0, gt=0)
    scheduler_lock_key: int = 1
    scheduler_catch_up_limit: int = Field(default=100, ge=1)
    # Realtime SSE stream (ADR-0010).
    stream_heartbeat_seconds: float = Field(default=15.0, gt=0)
    stream_queue_size: int = Field(default=256, ge=1)
    stream_poll_interval_seconds: float = Field(default=2.0, gt=0)
    stream_batch_size: int = Field(default=500, ge=1, le=1000)
    stream_replay_max_events: int = Field(default=1000, ge=1, le=10_000)
    stream_replay_max_age_seconds: float = Field(default=86_400.0, gt=0)
    stream_listen_backoff_base_seconds: float = Field(default=0.5, gt=0)
    stream_listen_backoff_cap_seconds: float = Field(default=30.0, gt=0)

    @property
    def lease(self) -> timedelta:
        return timedelta(seconds=self.lease_seconds)

    @property
    def retry_policy(self) -> RetryPolicy:
        return RetryPolicy(
            base_seconds=self.retry_base_seconds,
            cap_seconds=self.retry_cap_seconds,
            max_attempts=self.max_attempts,
        )
