"""API key format, parsing, hashing and scope mapping (pure)."""

import re

import pytest

from financial_hub.modules.identity.application.api import Permission
from financial_hub.modules.projects.application.scopes import (
    ADMIN_ONLY,
    SCOPE_PERMISSIONS,
    permissions_for_scopes,
)
from financial_hub.modules.projects.domain import (
    DUMMY_DIGEST,
    ApiKeyScope,
    NewKey,
    hash_secret,
    mint_key,
    parse_key,
    secret_matches,
    validate_key_name,
    validate_scopes,
)
from financial_hub.modules.projects.domain.api_keys import PREFIX_LENGTH, SECRET_LENGTH

PEPPER = b"unit-test-pepper-0123456789abcdef"


@pytest.mark.parametrize("environment", ["test", "live"])
def test_a_minted_key_has_the_documented_shape(environment: str) -> None:
    key = mint_key("live" if environment == "live" else "test")

    assert re.fullmatch(rf"fh_{environment}_[a-z0-9]{{8}}_[0-9A-Za-z]{{43}}", key.plaintext)
    assert len(key.prefix) == PREFIX_LENGTH
    assert len(key.secret) == SECRET_LENGTH


def test_minted_keys_are_unique() -> None:
    keys = [mint_key("test") for _ in range(200)]

    assert len({key.secret for key in keys}) == 200
    assert len({key.prefix for key in keys}) == 200


def test_a_minted_key_carries_at_least_256_bits() -> None:
    # 43 base62 digits cover 32 random bytes: 62**43 > 2**256.
    assert 62**SECRET_LENGTH > 2**256


def test_parse_round_trips() -> None:
    key = mint_key("live")
    parsed = parse_key(key.plaintext)

    assert parsed is not None
    assert (parsed.environment, parsed.prefix, parsed.secret) == ("live", key.prefix, key.secret)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "fh_",
        "fh_test_abcd1234",
        "fh_prod_abcd1234_" + "a" * 43,  # unknown environment
        "fh_test_ABCD1234_" + "a" * 43,  # prefix is lower-case
        "fh_test_abcd123_" + "a" * 43,  # prefix too short
        "fh_test_abcd1234_" + "a" * 42,  # secret too short
        "fh_test_abcd1234_" + "a" * 44,  # secret too long
        "fh_test_abcd1234_" + "a" * 42 + "-",  # outside base62
        "xx_test_abcd1234_" + "a" * 43,
        " fh_test_abcd1234_" + "a" * 43,
        "fh_test_abcd1234_" + "a" * 43 + "\n",
    ],
)
def test_malformed_keys_do_not_parse(value: str) -> None:
    assert parse_key(value) is None


def test_the_digest_is_hmac_sha256_of_the_secret_under_the_pepper() -> None:
    digest = hash_secret(PEPPER, "secret")

    assert len(digest) == 32
    assert digest == hash_secret(PEPPER, "secret")
    assert digest != hash_secret(PEPPER, "secreT")
    assert digest != hash_secret(b"another-pepper-0123456789abcdef!", "secret")
    assert b"secret" not in digest


def test_secret_matches_is_exact() -> None:
    stored = hash_secret(PEPPER, "right")

    assert secret_matches(PEPPER, "right", stored) is True
    assert secret_matches(PEPPER, "wrong", stored) is False
    assert secret_matches(PEPPER, "right", DUMMY_DIGEST) is False
    assert len(DUMMY_DIGEST) == 32


def test_secret_comparison_is_constant_time() -> None:
    import inspect

    from financial_hub.modules.projects.domain import api_keys

    assert "hmac.compare_digest" in inspect.getsource(api_keys.secret_matches)


def test_the_plaintext_is_hidden_from_repr() -> None:
    key = mint_key("test")

    assert key.secret not in repr(key)
    assert key.secret not in str(key)
    assert "<hidden>" in repr(key)
    assert isinstance(key, NewKey)


def test_names_are_trimmed_and_bounded() -> None:
    assert validate_key_name("  server  ") == "server"
    for bad in ("", "   ", "x" * 101):
        with pytest.raises(ValueError, match="name"):
            validate_key_name(bad)


def test_scopes_are_known_distinct_and_non_empty() -> None:
    assert validate_scopes(["ledger:read", "projects:read", "ledger:read"]) == (
        ApiKeyScope.LEDGER_READ,
        ApiKeyScope.PROJECTS_READ,
    )
    with pytest.raises(ValueError, match="at least one"):
        validate_scopes([])
    with pytest.raises(ValueError, match="unknown scope"):
        validate_scopes(["ledger:read", "api_keys:manage"])


def test_every_scope_maps_to_permissions() -> None:
    assert set(SCOPE_PERMISSIONS) == set(ApiKeyScope)


@pytest.mark.parametrize("scope", list(ApiKeyScope))
def test_no_scope_grants_an_administrative_permission(scope: ApiKeyScope) -> None:
    assert permissions_for_scopes([scope]) & ADMIN_ONLY == frozenset()


def test_scopes_grant_exactly_their_permissions() -> None:
    assert permissions_for_scopes([ApiKeyScope.LEDGER_READ]) == {Permission.LEDGER_READ.value}
    assert permissions_for_scopes([ApiKeyScope.CUSTOMERS_WRITE]) == {
        Permission.CUSTOMERS_READ.value,
        Permission.CUSTOMERS_MANAGE.value,
    }
    assert permissions_for_scopes([]) == frozenset()


def test_the_admin_only_set_covers_key_provider_and_member_management() -> None:
    assert {
        Permission.API_KEYS_MANAGE.value,
        Permission.INTEGRATIONS_MANAGE.value,
        Permission.INTEGRATIONS_READ.value,
        Permission.MEMBERS_MANAGE.value,
        Permission.PROJECTS_MANAGE.value,
    } <= ADMIN_ONLY
