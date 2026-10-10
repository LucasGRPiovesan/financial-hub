import base64

import pytest
from pydantic import ValidationError

from financial_hub.platform.settings import Settings, get_settings
from financial_hub.platform.telemetry import configure_telemetry

DB_URL = "postgresql+psycopg://u:p@db:5432/x"
KEY = base64.b64encode(b"\x01" * 32).decode()


def make(**values: object) -> Settings:
    return Settings(_env_file=None, database_url=DB_URL, **values)  # pyright: ignore[reportCallIssue]


def test_reads_prefixed_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FH_APP_ENV", "test")
    monkeypatch.setenv("FH_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("FH_OTEL_EXPORTER_OTLP_ENDPOINT", "http://lgtm:4318/")
    monkeypatch.setenv("FH_ENCRYPTION_KEYS", f'{{"1": "{KEY}", "2": "{KEY}"}}')

    settings = make()

    assert settings.app_env == "test"
    assert settings.log_level == "DEBUG"
    assert settings.otel_exporter_otlp_endpoint == "http://lgtm:4318"
    assert settings.active_encryption_key_version == 2


def test_secrets_are_not_rendered() -> None:
    settings = make(encryption_keys={1: KEY})

    assert "p@db" not in repr(settings)
    assert KEY not in repr(settings)
    assert settings.database_url.get_secret_value() == DB_URL


@pytest.mark.parametrize("value", ["", "   ", None])
def test_blank_otel_endpoint_disables_export(value: str | None) -> None:
    settings = make(otel_exporter_otlp_endpoint=value)

    assert settings.otel_exporter_otlp_endpoint is None
    assert configure_telemetry(settings) is None


def test_no_encryption_keys_by_default() -> None:
    assert make().active_encryption_key_version is None


@pytest.mark.parametrize(
    ("key", "message"),
    [("not base64!", "not valid base64"), (base64.b64encode(b"short").decode(), "32 bytes")],
)
def test_encryption_keys_must_be_aes256(key: str, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        make(encryption_keys={1: key})


def test_database_url_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FH_DATABASE_URL", raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # pyright: ignore[reportCallIssue]


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()
