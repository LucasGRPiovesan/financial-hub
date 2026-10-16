"""Standard Webhooks signing (https://www.standardwebhooks.com/, docs/INTEGRATIONS.md s3).

The secret is ``whsec_`` + base64 of 32 random bytes; the HMAC key is the decoded bytes. Signed
content is ``{webhook-id}.{webhook-timestamp}.{body}`` and a signature is ``v1,`` + base64 of its
HMAC-SHA256. During a rotation overlap the ``webhook-signature`` header carries one signature
per valid secret, space-separated; a receiver accepts the message when any of them verifies.
"""

import base64
import binascii
import hashlib
import hmac
import secrets
from collections.abc import Iterable
from typing import Final
from uuid import UUID

SECRET_PREFIX: Final = "whsec_"  # noqa: S105 - a prefix, not a secret
SECRET_BYTES: Final = 32
SIGNATURE_VERSION: Final = "v1"


class InvalidWebhookSecretError(ValueError):
    """The value is not a ``whsec_`` + base64 secret."""


def generate_secret() -> str:
    return SECRET_PREFIX + base64.b64encode(secrets.token_bytes(SECRET_BYTES)).decode("ascii")


def secret_key(secret: str) -> bytes:
    """HMAC key of a ``whsec_`` secret (the base64-decoded part after the prefix)."""
    if not secret.startswith(SECRET_PREFIX):
        raise InvalidWebhookSecretError("webhook secrets start with whsec_")
    try:
        key = base64.b64decode(secret.removeprefix(SECRET_PREFIX), validate=True)
    except binascii.Error:
        raise InvalidWebhookSecretError("webhook secret is not valid base64") from None
    if not key:
        raise InvalidWebhookSecretError("webhook secret is empty")
    return key


def sign(secret: str, *, message_id: str, timestamp: int, body: bytes) -> str:
    """``v1,<base64 HMAC-SHA256>`` of ``{message_id}.{timestamp}.{body}``."""
    content = f"{message_id}.{timestamp}.".encode() + body
    digest = hmac.new(secret_key(secret), content, hashlib.sha256).digest()
    return f"{SIGNATURE_VERSION}," + base64.b64encode(digest).decode("ascii")


def signature_header(
    secrets_in_order: Iterable[str], *, message_id: str, timestamp: int, body: bytes
) -> str:
    """The ``webhook-signature`` value: one signature per secret, current one first."""
    return " ".join(
        sign(secret, message_id=message_id, timestamp=timestamp, body=body)
        for secret in secrets_in_order
    )


def verify(secret: str, header: str, *, message_id: str, timestamp: int, body: bytes) -> bool:
    """Receiver-side check (used by tests and docs): any ``v1`` signature matches."""
    expected = sign(secret, message_id=message_id, timestamp=timestamp, body=body)
    return any(hmac.compare_digest(expected, candidate) for candidate in header.split())


def secret_aad(endpoint_id: UUID) -> bytes:
    """AAD of a sealed endpoint secret: a ciphertext copied to another endpoint never opens."""
    return f"webhook_endpoint|{endpoint_id}".encode()
