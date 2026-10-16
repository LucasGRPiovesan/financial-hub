"""Pure audit domain: redaction of secret-looking keys and before/after diffs."""

import pytest

from financial_hub.modules.audit.domain import REDACTED, Actor, ActorType, changed_fields, redact


@pytest.mark.parametrize(
    "key",
    [
        "secret",
        "secret_hash",
        "ciphertext",
        "password",
        "access_token",
        "credentials",
        "nonce",
        "api_key",
        "apiKey",
        "key",
        "cpf",
        "cnpj",
        "document",
        "PLAINTEXT",
    ],
)
def test_sensitive_keys_are_masked(key: str) -> None:
    assert redact({key: "value"}) == {key: REDACTED}


def test_ordinary_keys_pass_through_and_nesting_is_walked() -> None:
    value = {
        "name": "x",
        "nested": {"scopes": ["a"], "token": "t", "items": [{"hash": "h", "n": 1}]},
    }
    assert redact(value) == {
        "name": "x",
        "nested": {"scopes": ["a"], "token": REDACTED, "items": [{"hash": REDACTED, "n": 1}]},
    }


def test_a_sensitive_key_masks_a_whole_subtree() -> None:
    assert redact({"credentials": {"api_key": "k", "other": 1}}) == {"credentials": REDACTED}


def test_non_mappings_are_returned_unchanged() -> None:
    assert redact("plain") == "plain"
    assert redact(3) == 3
    assert redact((1, 2)) == [1, 2]


def test_changed_fields_keeps_only_what_differs() -> None:
    assert changed_fields({"a": 1, "b": 2}, {"a": 1, "b": 3, "c": 4}) == {
        "before": {"b": 2, "c": None},
        "after": {"b": 3, "c": 4},
    }


def test_changed_fields_for_creation_and_removal() -> None:
    assert changed_fields(None, {"a": 1}) == {"after": {"a": 1}}
    assert changed_fields({"a": 1}, None) == {"before": {"a": 1}}
    assert changed_fields(None, None) == {"after": {}}


def test_actors() -> None:
    assert Actor.system() == Actor(ActorType.SYSTEM, None)
    assert Actor.of_principal("operator", "opr_1") == Actor(ActorType.OPERATOR, "opr_1")
    assert Actor.of_principal("api_key", "key_1") == Actor(ActorType.API_KEY, "key_1")
    assert Actor.of_principal("alien", "x") == Actor(ActorType.SYSTEM, "x")
