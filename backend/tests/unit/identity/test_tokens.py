import hashlib

import pytest

from financial_hub.modules.identity.application import tokens


def test_session_tokens_are_random_43_char_base64url() -> None:
    first, raw = tokens.new_session_token()
    second, _ = tokens.new_session_token()
    assert len(first) == 43
    assert first != second
    assert len(raw) == 32
    assert tokens.parse_session_token(first) == raw


@pytest.mark.parametrize(
    "value",
    [None, "", "short", "A" * 42, "A" * 44, "A" * 42 + "=", "A" * 42 + "+", "A" * 42 + "B"],
)
def test_malformed_or_non_canonical_tokens_are_rejected(value: str | None) -> None:
    # "A"*42+"B" decodes, but has non-zero unused bits: not the canonical spelling of any token.
    assert tokens.parse_session_token(value) is None


def test_session_id_is_the_sha256_of_the_raw_token() -> None:
    _, raw = tokens.new_session_token()
    assert tokens.session_id(raw) == hashlib.sha256(raw).digest()


def test_csrf_token_is_bound_to_the_session() -> None:
    _, first = tokens.new_session_token()
    _, second = tokens.new_session_token()
    token = tokens.csrf_token_for(first)
    assert token == tokens.csrf_token_for(first)
    assert token != tokens.csrf_token_for(second)
    assert len(token) == 43


def test_csrf_matching() -> None:
    _, raw = tokens.new_session_token()
    token = tokens.csrf_token_for(raw)
    stored = tokens.csrf_token_hash(token)
    assert tokens.csrf_matches(stored, token)
    assert not tokens.csrf_matches(stored, None)
    assert not tokens.csrf_matches(stored, "")
    assert not tokens.csrf_matches(stored, token[:-1] + ("A" if token[-1] != "A" else "B"))
