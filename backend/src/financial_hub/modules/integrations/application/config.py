"""Integrations settings from ``FH_INTEGRATIONS_*`` environment variables."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class IntegrationsSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FH_INTEGRATIONS_", env_file=".env", extra="ignore"
    )

    # Four-eyes (approver != author) is always required when a manifest changes splits, prices or
    # the wallet/fee policy. Set this to require it for every approval, even description-only.
    four_eyes_always: bool = False
