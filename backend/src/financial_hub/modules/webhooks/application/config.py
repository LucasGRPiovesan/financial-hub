"""Webhook sender tuning from ``FH_WEBHOOK_*`` environment variables."""

from datetime import timedelta

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from financial_hub.modules.webhooks.domain import TargetPolicy


class WebhookSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FH_WEBHOOK_", env_file=".env", extra="ignore")

    # Dev/test only (ignored elsewhere): comma-separated host names or IP literals that may be
    # private, e.g. a local receiver for tests and e2e. Outside dev private targets are refused.
    allow_hosts: str = ""
    sender_enabled: bool = True
    batch_size: int = Field(default=10, ge=1, le=100)
    # A lease must outlast a whole batch of sends (batch_size x timeout) or deliveries are
    # released unsent at the end of the batch and picked up again on the next claim.
    lease_seconds: float = Field(default=300.0, gt=0)
    timeout_seconds: float = Field(default=10.0, gt=0, le=30)
    poll_interval_seconds: float = Field(default=2.0, gt=0)

    @property
    def lease(self) -> timedelta:
        return timedelta(seconds=self.lease_seconds)

    def target_policy(self, *, dev: bool) -> TargetPolicy:
        hosts = frozenset(h.strip() for h in self.allow_hosts.split(",") if h.strip())
        return TargetPolicy.for_environment(dev=dev, allowed_hosts=hosts)
