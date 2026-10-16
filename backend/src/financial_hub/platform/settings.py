import base64
import binascii
import ipaddress
from decimal import Decimal
from functools import lru_cache
from typing import Final, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENCRYPTION_KEY_BYTES = 32  # AES-256-GCM (docs/SECURITY.md)

# Environments that may run on development defaults (no pepper, no data keys). Every other
# environment is deployed and must configure its secrets explicitly.
DEV_ENVIRONMENTS: Final = frozenset({"dev", "test"})
# Deployments whose API keys are ``live``; all others mint and accept ``test`` keys.
LIVE_ENVIRONMENTS: Final = frozenset({"prod", "production", "live"})
# Clearly fake: the default in dev/test, rejected everywhere else.
DEV_API_KEY_PEPPER: Final = "dev-only-INSECURE-api-key-pepper-do-not-use"
API_KEY_PEPPER_MIN_LENGTH: Final = 32


class Settings(BaseSettings):
    """Process configuration from ``FH_*`` environment variables (or a local ``.env``)."""

    # env_ignore_empty: compose forwards unset optional secrets as "" (meaning: use the default).
    model_config = SettingsConfigDict(
        env_prefix="FH_", env_file=".env", extra="ignore", env_ignore_empty=True
    )

    app_env: str = "dev"
    log_level: str = "INFO"
    database_url: SecretStr
    # OTLP/HTTP base URL (e.g. http://lgtm:4318). Unset or empty disables telemetry export.
    otel_exporter_otlp_endpoint: str | None = None
    otel_service_name: str = "financial-hub-api"
    # Versioned data keys, JSON: {"1": "<base64 of 32 bytes>", ...}. Highest version encrypts.
    encryption_keys: dict[int, SecretStr] = {}
    # HMAC-SHA256 key for API key secrets (docs/SECURITY.md): a database read alone can neither
    # verify nor brute-force a stolen digest without it. Non-default outside dev/test.
    api_key_pepper: SecretStr = SecretStr(DEV_API_KEY_PEPPER)
    # An API key's last_used_at is written at most once per interval.
    api_key_touch_interval_seconds: int = Field(default=60, ge=0)

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

    # Demo login (docs/SECURITY.md): lets any visitor open a session as any active operator without
    # a password. Evaluation only; off by default and never enabled in production.
    demo_login: bool = False

    # Idempotency-Key (ADR-0004): how long a key is remembered, and the largest response body
    # stored for replay (bigger ones are recorded without a body, see docs/ARCHITECTURE.md).
    idempotency_ttl_hours: int = Field(default=24, gt=0)
    idempotency_max_body_bytes: int = Field(default=1024 * 1024, ge=0)
    # Rate limiting (docs/SECURITY.md): Postgres token buckets, capacity = burst size, refill in
    # tokens per second (decimal, e.g. "0.5"; parsed exactly, never as a float).
    rate_limit_enabled: bool = True
    rate_limit_api_key_capacity: int = Field(default=600, gt=0)
    rate_limit_api_key_refill_per_second: Decimal = Field(default=Decimal(10), gt=0)
    rate_limit_operator_capacity: int = Field(default=300, gt=0)
    rate_limit_operator_refill_per_second: Decimal = Field(default=Decimal(5), gt=0)
    # Unauthenticated auth attempts per client IP: logins and rejected credentials.
    rate_limit_auth_ip_capacity: int = Field(default=60, gt=0)
    rate_limit_auth_ip_refill_per_second: Decimal = Field(default=Decimal("0.1"), gt=0)
    # Proxies (IPs or CIDRs) whose X-Forwarded-For is believed; empty = the socket peer is the
    # client and X-Forwarded-For is ignored.
    rate_limit_trusted_proxies: list[str] = []

    # Payments (PRO-40): the scriptable "fake" provider (no money moves). Unset = on in dev/test,
    # off elsewhere; it can never be enabled in a live environment.
    payments_fake_provider_enabled: bool | None = None

    @field_validator("otel_exporter_otlp_endpoint")
    @classmethod
    def _blank_endpoint_is_unset(cls, value: str | None) -> str | None:
        return value.strip().rstrip("/") or None if value is not None else None

    @field_validator("rate_limit_trusted_proxies")
    @classmethod
    def _proxies_are_networks(cls, value: list[str]) -> list[str]:
        for entry in value:
            try:
                ipaddress.ip_network(entry, strict=False)
            except ValueError:
                raise ValueError(f"trusted proxy {entry!r} is not an IP address or CIDR") from None
        return value

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

    @model_validator(mode="after")
    def _deployed_environments_configure_secrets(self) -> Settings:
        if self.payments_fake_provider_enabled and self.app_env in LIVE_ENVIRONMENTS:
            raise ValueError("FH_PAYMENTS_FAKE_PROVIDER_ENABLED must not be true in live")
        if self.app_env in DEV_ENVIRONMENTS:
            return self
        pepper = self.api_key_pepper.get_secret_value()
        if pepper == DEV_API_KEY_PEPPER or len(pepper) < API_KEY_PEPPER_MIN_LENGTH:
            raise ValueError(
                f"FH_API_KEY_PEPPER must be a secret of at least {API_KEY_PEPPER_MIN_LENGTH} "
                f"characters when FH_APP_ENV is {self.app_env!r}"
            )
        if not self.encryption_keys:
            raise ValueError(f"FH_ENCRYPTION_KEYS must be set when FH_APP_ENV is {self.app_env!r}")
        return self

    @property
    def api_key_environment(self) -> Literal["test", "live"]:
        """The ``<env>`` segment of the API keys this deployment mints and accepts."""
        return "live" if self.app_env in LIVE_ENVIRONMENTS else "test"

    @property
    def active_encryption_key_version(self) -> int | None:
        return max(self.encryption_keys, default=None)

    @property
    def fake_payment_provider_enabled(self) -> bool:
        """Whether the "fake" payment provider may be used (``payments_fake_provider_enabled``)."""
        if self.payments_fake_provider_enabled is None:
            return self.app_env in DEV_ENVIRONMENTS
        return self.payments_fake_provider_enabled

    @property
    def cookies_secure(self) -> bool:
        """``Secure`` cookies everywhere except local dev over plain http."""
        return self.app_env != "dev"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # pyright: ignore[reportCallIssue]
