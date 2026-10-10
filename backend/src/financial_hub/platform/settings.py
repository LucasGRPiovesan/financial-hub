import base64
import binascii
from functools import lru_cache

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENCRYPTION_KEY_BYTES = 32  # AES-256-GCM (docs/SECURITY.md)


class Settings(BaseSettings):
    """Process configuration from ``FH_*`` environment variables (or a local ``.env``)."""

    model_config = SettingsConfigDict(env_prefix="FH_", env_file=".env", extra="ignore")

    app_env: str = "dev"
    log_level: str = "INFO"
    database_url: SecretStr
    # OTLP/HTTP base URL (e.g. http://lgtm:4318). Unset or empty disables telemetry export.
    otel_exporter_otlp_endpoint: str | None = None
    otel_service_name: str = "financial-hub-api"
    # Versioned data keys, JSON: {"1": "<base64 of 32 bytes>", ...}. Highest version encrypts.
    encryption_keys: dict[int, SecretStr] = {}

    @field_validator("otel_exporter_otlp_endpoint")
    @classmethod
    def _blank_endpoint_is_unset(cls, value: str | None) -> str | None:
        return value.strip().rstrip("/") or None if value is not None else None

    @field_validator("encryption_keys")
    @classmethod
    def _keys_are_aes256(cls, value: dict[int, SecretStr]) -> dict[int, SecretStr]:
        for version, key in value.items():
            try:
                raw = base64.b64decode(key.get_secret_value(), validate=True)
            except binascii.Error:
                raise ValueError(f"encryption key {version} is not valid base64") from None
            if len(raw) != ENCRYPTION_KEY_BYTES:
                raise ValueError(f"encryption key {version} must be {ENCRYPTION_KEY_BYTES} bytes")
        return value

    @property
    def active_encryption_key_version(self) -> int | None:
        return max(self.encryption_keys, default=None)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # pyright: ignore[reportCallIssue]
