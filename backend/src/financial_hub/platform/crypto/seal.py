"""Encryption half of the secrets-at-rest primitive (AES-256-GCM)."""

import os
from dataclasses import dataclass, field

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from financial_hub.platform.crypto.keyring import NONCE_BYTES, Keyring


@dataclass(frozen=True, slots=True)
class SealedSecret:
    """What is stored per row: the key version, the random nonce and ciphertext||tag."""

    key_version: int
    nonce: bytes
    ciphertext: bytes = field(repr=False)


def seal(keyring: Keyring, plaintext: bytes, *, aad: bytes) -> SealedSecret:
    """Encrypt under the active key with a fresh random 96-bit nonce.

    ``aad`` is authenticated, not stored: bind it to the owning row (project, provider, id) so a
    ciphertext copied to another row no longer opens. Random nonces are safe for the volumes here
    (collision odds stay negligible far below 2**32 encryptions per key); rotate keys long before.
    """
    version = keyring.active_version
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(keyring.key(version)).encrypt(nonce, plaintext, aad)
    return SealedSecret(key_version=version, nonce=nonce, ciphertext=ciphertext)
