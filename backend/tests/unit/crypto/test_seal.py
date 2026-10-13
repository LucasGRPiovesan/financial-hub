"""AES-256-GCM sealing: round trip, versioned keyring, and every kind of tampering."""

import dataclasses

import pytest

from financial_hub.platform.crypto.keyring import (
    NONCE_BYTES,
    DecryptionError,
    Keyring,
    KeyringError,
)
from financial_hub.platform.crypto.seal import SealedSecret, seal
from financial_hub.platform.crypto.unseal import unseal

KEY_1 = bytes(range(32))
KEY_2 = bytes(range(32, 64))
AAD = b"project|asaas|account"
PLAINTEXT = b'{"api_key":"sk_live_do_not_log_me"}'


def keyring(**versions: bytes) -> Keyring:
    return Keyring({int(name.removeprefix("v")): key for name, key in versions.items()})


def test_round_trip() -> None:
    ring = keyring(v1=KEY_1)
    sealed = seal(ring, PLAINTEXT, aad=AAD)

    assert unseal(ring, sealed, aad=AAD) == PLAINTEXT
    assert sealed.key_version == 1
    assert len(sealed.nonce) == NONCE_BYTES
    assert PLAINTEXT not in sealed.ciphertext
    assert len(sealed.ciphertext) == len(PLAINTEXT) + 16  # GCM tag


def test_every_encryption_uses_a_fresh_nonce() -> None:
    ring = keyring(v1=KEY_1)
    sealed = [seal(ring, PLAINTEXT, aad=AAD) for _ in range(50)]

    assert len({item.nonce for item in sealed}) == 50
    assert len({item.ciphertext for item in sealed}) == 50


def test_the_highest_version_encrypts_and_old_versions_still_open() -> None:
    old = seal(keyring(v1=KEY_1), PLAINTEXT, aad=AAD)
    both = keyring(v1=KEY_1, v2=KEY_2)
    new = seal(both, PLAINTEXT, aad=AAD)

    assert both.active_version == 2
    assert (old.key_version, new.key_version) == (1, 2)
    assert unseal(both, old, aad=AAD) == unseal(both, new, aad=AAD) == PLAINTEXT


def flip(data: bytes) -> bytes:
    return bytes([data[0] ^ 1]) + data[1:]


def test_tampered_ciphertext_fails() -> None:
    ring = keyring(v1=KEY_1)
    sealed = seal(ring, PLAINTEXT, aad=AAD)
    forged = dataclasses.replace(sealed, ciphertext=flip(sealed.ciphertext))

    with pytest.raises(DecryptionError):
        unseal(ring, forged, aad=AAD)


def test_tampered_tag_fails() -> None:
    ring = keyring(v1=KEY_1)
    sealed = seal(ring, PLAINTEXT, aad=AAD)
    forged = dataclasses.replace(sealed, ciphertext=sealed.ciphertext[:-1] + b"\x00")

    with pytest.raises(DecryptionError):
        unseal(ring, forged, aad=AAD)


def test_tampered_nonce_fails() -> None:
    ring = keyring(v1=KEY_1)
    sealed = seal(ring, PLAINTEXT, aad=AAD)
    forged = dataclasses.replace(sealed, nonce=flip(sealed.nonce))

    with pytest.raises(DecryptionError):
        unseal(ring, forged, aad=AAD)


@pytest.mark.parametrize("length", [0, NONCE_BYTES - 1, NONCE_BYTES + 1])
def test_a_nonce_of_the_wrong_length_fails(length: int) -> None:
    ring = keyring(v1=KEY_1)
    sealed = seal(ring, PLAINTEXT, aad=AAD)
    forged = dataclasses.replace(sealed, nonce=b"\x01" * length)

    with pytest.raises(DecryptionError):
        unseal(ring, forged, aad=AAD)


@pytest.mark.parametrize(
    "other_aad",
    [b"other-project|asaas|account", b"project|stripe|account", b"project|asaas|other", b""],
)
def test_swapping_the_aad_fails(other_aad: bytes) -> None:
    """A ciphertext copied to another project/provider/account row no longer opens."""
    ring = keyring(v1=KEY_1)
    sealed = seal(ring, PLAINTEXT, aad=AAD)

    with pytest.raises(DecryptionError):
        unseal(ring, sealed, aad=other_aad)


def test_a_different_key_or_unknown_version_fails() -> None:
    sealed = seal(keyring(v1=KEY_1), PLAINTEXT, aad=AAD)

    with pytest.raises(DecryptionError):
        unseal(keyring(v1=KEY_2), sealed, aad=AAD)
    with pytest.raises(DecryptionError):
        unseal(keyring(v2=KEY_2), sealed, aad=AAD)
    with pytest.raises(DecryptionError):
        unseal(keyring(v1=KEY_1), dataclasses.replace(sealed, key_version=2), aad=AAD)


def test_decryption_errors_carry_no_detail() -> None:
    sealed = seal(keyring(v1=KEY_1), PLAINTEXT, aad=AAD)
    with pytest.raises(DecryptionError) as raised:
        unseal(keyring(v1=KEY_1), sealed, aad=b"x")

    assert raised.value.args == ()


def test_an_empty_keyring_cannot_seal() -> None:
    empty = Keyring({})

    assert empty.configured is False
    with pytest.raises(KeyringError):
        seal(empty, PLAINTEXT, aad=AAD)


def test_unknown_key_version_is_a_keyring_error() -> None:
    with pytest.raises(KeyringError, match="version 3"):
        keyring(v1=KEY_1).key(3)


@pytest.mark.parametrize("entries", [{0: KEY_1}, {-1: KEY_1}, {1: KEY_1[:16]}, {1: KEY_1 + b"x"}])
def test_the_keyring_rejects_malformed_entries(entries: dict[int, bytes]) -> None:
    with pytest.raises(KeyringError):
        Keyring(entries)


def test_the_keyring_never_reveals_key_material() -> None:
    ring = keyring(v1=KEY_1, v2=KEY_2)

    assert repr(ring) == "Keyring(versions=[1, 2])"
    for key in (KEY_1, KEY_2):
        assert repr(key) not in repr(ring)


def test_a_sealed_secret_repr_hides_the_ciphertext() -> None:
    sealed = SealedSecret(key_version=1, nonce=b"n" * NONCE_BYTES, ciphertext=b"super-secret-bytes")

    assert "super-secret-bytes" not in repr(sealed)
