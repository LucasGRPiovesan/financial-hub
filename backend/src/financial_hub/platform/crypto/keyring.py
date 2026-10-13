"""The data keys: AES-256 keys by version (``FH_ENCRYPTION_KEYS``); the highest encrypts.

Rotation = add a new version and restart; rows sealed under older versions keep decrypting because
each row stores the version that sealed it. Re-encrypting old rows is *Planned* (docs/SECURITY.md).
"""

import base64
from collections.abc import Mapping
from typing import Final

from financial_hub.platform.settings import ENCRYPTION_KEY_BYTES, Settings

NONCE_BYTES: Final = 12  # 96-bit GCM nonce, random per encryption


class KeyringError(RuntimeError):
    """The keyring cannot serve the request (no keys configured, unknown version)."""


class DecryptionError(Exception):
    """Authenticated decryption failed: wrong key/version, or tampered ciphertext, nonce or AAD.

    Carries no detail on purpose: which part failed is not something a caller may learn.
    """


class Keyring:
    def __init__(self, keys: Mapping[int, bytes]) -> None:
        for version, key in keys.items():
            if version < 1 or len(key) != ENCRYPTION_KEY_BYTES:
                raise KeyringError("keyring entries need a positive version and a 32-byte key")
        self._keys: dict[int, bytes] = dict(keys)

    @classmethod
    def from_settings(cls, settings: Settings) -> Keyring:
        return cls(
            {
                version: base64.b64decode(key.get_secret_value())
                for version, key in settings.encryption_keys.items()
            }
        )

    @property
    def active_version(self) -> int:
        if not self._keys:
            raise KeyringError("no encryption keys are configured (FH_ENCRYPTION_KEYS)")
        return max(self._keys)

    @property
    def configured(self) -> bool:
        return bool(self._keys)

    def key(self, version: int) -> bytes:
        try:
            return self._keys[version]
        except KeyError:
            raise KeyringError(f"encryption key version {version} is not configured") from None

    def __repr__(self) -> str:
        return f"Keyring(versions={sorted(self._keys)})"
