"""Deployed environments must configure the API key pepper and the data keys explicitly."""

import base64

import pytest
from pydantic import SecretStr, ValidationError

from financial_hub.platform.crypto.keyring import Keyring
from financial_hub.platform.settings import DEV_API_KEY_PEPPER, Settings

DB_URL = "postgresql+psycopg://u:p@db:5432/x"
KEY = base64.b64encode(bytes([7]) * 32).decode()
PEPPER = "p" * 40


def make(**values: object) -> Settings:
    return Settings(_env_file=None, database_url=DB_URL, **values)  # pyright: ignore[reportCallIssue]


@pytest.mark.parametrize("env", ["dev", "test"])
def test_dev_and_test_run_on_defaults(env: str) -> None:
    settings = make(app_env=env)

    assert settings.api_key_pepper.get_secret_value() == DEV_API_KEY_PEPPER
    assert settings.api_key_environment == "test"
    assert settings.api_key_touch_interval_seconds == 60


def test_a_deployed_environment_needs_a_real_pepper() -> None:
    with pytest.raises(ValidationError, match="FH_API_KEY_PEPPER"):
        make(app_env="prod", encryption_keys={1: KEY})
    with pytest.raises(ValidationError, match="FH_API_KEY_PEPPER"):
        make(app_env="prod", encryption_keys={1: KEY}, api_key_pepper="short")
    with pytest.raises(ValidationError, match="FH_API_KEY_PEPPER"):
        make(app_env="staging", encryption_keys={1: KEY}, api_key_pepper=DEV_API_KEY_PEPPER)


def test_a_deployed_environment_needs_data_keys() -> None:
    with pytest.raises(ValidationError, match="FH_ENCRYPTION_KEYS"):
        make(app_env="prod", api_key_pepper=PEPPER)


@pytest.mark.parametrize(
    ("env", "expected"), [("prod", "live"), ("production", "live"), ("staging", "test")]
)
def test_the_key_environment_follows_the_deployment(env: str, expected: str) -> None:
    settings = make(app_env=env, api_key_pepper=PEPPER, encryption_keys={1: KEY})

    assert settings.api_key_environment == expected


def test_the_pepper_and_keys_never_appear_in_a_repr() -> None:
    settings = make(app_env="prod", api_key_pepper=PEPPER, encryption_keys={1: KEY})

    assert PEPPER not in repr(settings)
    assert KEY not in repr(settings)
    assert isinstance(settings.api_key_pepper, SecretStr)


def test_the_keyring_is_built_from_settings() -> None:
    ring = Keyring.from_settings(make(encryption_keys={1: KEY, 3: KEY}))

    assert ring.active_version == 3
    assert ring.key(1) == bytes([7]) * 32
    assert Keyring.from_settings(make()).configured is False
