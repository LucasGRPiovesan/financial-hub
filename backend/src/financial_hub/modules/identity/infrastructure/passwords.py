"""Argon2id password hashing (argon2-cffi).

Parameters follow the OWASP Password Storage Cheat Sheet baseline for Argon2id: m = 19 MiB
(19456 KiB), t = 2 iterations, p = 1 lane; 16-byte random salt, 32-byte tag. Hashes are PHC strings
(``$argon2id$v=19$m=19456,t=2,p=1$...``) so the parameters travel with every hash: raising them
later only needs ``needs_rehash`` (checked on each successful login).
"""

import secrets
from typing import Final

from argon2 import PasswordHasher as Argon2Hasher
from argon2 import Type
from argon2.exceptions import InvalidHashError, VerificationError

TIME_COST: Final = 2
MEMORY_COST_KIB: Final = 19 * 1024
PARALLELISM: Final = 1
HASH_LENGTH: Final = 32
SALT_LENGTH: Final = 16


class Argon2PasswordHasher:
    def __init__(
        self,
        *,
        time_cost: int = TIME_COST,
        memory_cost: int = MEMORY_COST_KIB,
        parallelism: int = PARALLELISM,
    ) -> None:
        self._hasher = Argon2Hasher(
            time_cost=time_cost,
            memory_cost=memory_cost,
            parallelism=parallelism,
            hash_len=HASH_LENGTH,
            salt_len=SALT_LENGTH,
            type=Type.ID,
        )
        self._dummy_hash: str | None = None

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, password_hash: str, password: str) -> bool:
        try:
            return self._hasher.verify(password_hash, password)
        except VerificationError, InvalidHashError:
            return False

    def needs_rehash(self, password_hash: str) -> bool:
        try:
            return self._hasher.check_needs_rehash(password_hash)
        except InvalidHashError:
            return True

    @property
    def dummy_hash(self) -> str:
        """Hash of an unguessable secret, verified for unknown emails so timing matches."""
        if self._dummy_hash is None:
            self._dummy_hash = self.hash(secrets.token_urlsafe(32))
        return self._dummy_hash
