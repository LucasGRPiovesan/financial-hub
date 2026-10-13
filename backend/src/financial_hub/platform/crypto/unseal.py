"""Decryption half of the secrets-at-rest primitive.

Only a module's ``infrastructure`` layer may import this module (import-linter contract in
``pyproject.toml``): application and API code can store secrets but never read them back.
"""

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from financial_hub.platform.crypto.keyring import (
    NONCE_BYTES,
    DecryptionError,
    Keyring,
    KeyringError,
)
from financial_hub.platform.crypto.seal import SealedSecret


def unseal(keyring: Keyring, sealed: SealedSecret, *, aad: bytes) -> bytes:
    """Decrypt and authenticate; ``DecryptionError`` for any mismatch (key, nonce, AAD, bytes)."""
    try:
        key = keyring.key(sealed.key_version)
    except KeyringError:
        raise DecryptionError from None
    if len(sealed.nonce) != NONCE_BYTES:
        raise DecryptionError
    try:
        return AESGCM(key).decrypt(sealed.nonce, sealed.ciphertext, aad)
    except InvalidTag, ValueError:
        raise DecryptionError from None
