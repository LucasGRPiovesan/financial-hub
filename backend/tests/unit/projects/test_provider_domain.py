"""Provider catalog, credential validation, encoding, hints and AAD (pure)."""

from uuid import uuid4

import pytest

from financial_hub.modules.projects.application.providers import provider_catalog
from financial_hub.modules.projects.domain import (
    CATALOG,
    Provider,
    ProviderNotEnabledError,
    credentials_aad,
    credentials_hint,
    decode_credentials,
    encode_credentials,
    require_enabled,
    validate_account_name,
    validate_credentials,
)


def test_only_asaas_is_enabled() -> None:
    assert {provider: spec.enabled for provider, spec in CATALOG.items()} == {
        Provider.ASAAS: True,
        Provider.STRIPE: False,
        Provider.MERCADOPAGO: False,
    }
    assert require_enabled(Provider.ASAAS).provider is Provider.ASAAS
    for provider in (Provider.STRIPE, Provider.MERCADOPAGO):
        with pytest.raises(ProviderNotEnabledError) as raised:
            require_enabled(provider)
        assert raised.value.provider is provider


def test_the_catalog_lists_every_provider_with_its_enabled_flag() -> None:
    entries = {entry.provider: entry for entry in provider_catalog()}

    assert set(entries) == {"asaas", "stripe", "mercadopago"}
    assert entries["asaas"].enabled is True
    assert entries["stripe"].enabled is False
    assert entries["asaas"].credential_fields == ("api_key",)
    assert entries["asaas"].environments == ("sandbox", "production")


def test_credentials_are_trimmed_and_checked_against_the_provider_spec() -> None:
    assert validate_credentials(Provider.ASAAS, {"api_key": "  key-value  "}) == {
        "api_key": "key-value"
    }
    assert validate_credentials(Provider.ASAAS, {"api_key": "k", "webhook_token": "t"}) == {
        "api_key": "k",
        "webhook_token": "t",
    }


def test_invalid_credentials_name_fields_but_never_values() -> None:
    secret = "sk_super_secret_value_1234"
    cases = [
        {},  # missing required
        {"api_key": secret, "surprise": secret},  # unknown field
        {"api_key": "   "},  # blank
        {"api_key": secret * 30},  # too long
    ]
    for credentials in cases:
        with pytest.raises(ValueError) as raised:
            validate_credentials(Provider.ASAAS, credentials)
        assert secret not in str(raised.value)


def test_encoding_is_canonical_and_round_trips() -> None:
    credentials = {"webhook_token": "t", "api_key": "k"}
    encoded = encode_credentials(credentials)

    assert encoded == b'{"api_key":"k","webhook_token":"t"}'
    assert decode_credentials(encoded) == credentials


def test_decoding_rejects_anything_but_an_object() -> None:
    with pytest.raises(ValueError, match="not an object"):
        decode_credentials(b'["a"]')


def test_the_hint_shows_four_characters_only_for_long_secrets() -> None:
    long_secret = "x" * 20 + "WXYZ"
    assert credentials_hint(Provider.ASAAS, {"api_key": long_secret}) == "WXYZ"
    assert credentials_hint(Provider.ASAAS, {"api_key": "short-1234"}) is None
    assert credentials_hint(Provider.ASAAS, {"api_key": "x" * 16}) == "xxxx"
    assert credentials_hint(Provider.ASAAS, {"api_key": "x" * 15}) is None


def test_the_aad_binds_project_provider_and_account() -> None:
    project, account = uuid4(), uuid4()

    assert credentials_aad(project, Provider.ASAAS, account) == (
        f"{project}|asaas|{account}".encode()
    )
    assert credentials_aad(project, Provider.ASAAS, account) != credentials_aad(
        uuid4(), Provider.ASAAS, account
    )
    assert credentials_aad(project, Provider.ASAAS, account) != credentials_aad(
        project, Provider.STRIPE, account
    )
    assert credentials_aad(project, Provider.ASAAS, account) != credentials_aad(
        project, Provider.ASAAS, uuid4()
    )


def test_account_names_are_trimmed_and_bounded() -> None:
    assert validate_account_name("  main  ") == "main"
    for bad in ("", "  ", "x" * 101):
        with pytest.raises(ValueError, match="name"):
            validate_account_name(bad)
