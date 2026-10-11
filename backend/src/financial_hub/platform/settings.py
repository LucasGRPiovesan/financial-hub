import base64
import binascii
from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
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

    # Operator sessions (docs/SECURITY.md): sliding idle timeout capped by an absolute lifetime.
    session_idle_timeout_minutes: int = Field(default=30, gt=0)
    session_absolute_timeout_hours: int = Field(default=12, gt=0)
    # last_seen_at/idle expiry are written at most once per interval per session.
    session_touch_interval_seconds: int = Field(default=60, ge=0)
    # Login throttling: consecutive failures per email (known or not) and attempts per client IP.
    login_max_failures: int = Field(default=5, gt=0)
    login_failure_window_minutes: int = Field(default=15, gt=0)
    login_lockout_minutes: int = Field(default=15, gt=0)
    login_ip_max_attempts: int = Field(default=30, gt=0)
    login_ip_window_minutes: int = Field(default=15, gt=0)

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

    @property
    def cookies_secure(self) -> bool:
        """``Secure`` cookies everywhere except local dev over plain http."""
        return self.app_env != "dev"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # pyright: ignore[reportCallIssue]
