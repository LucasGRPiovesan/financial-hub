"""Session and CSRF token primitives (docs/SECURITY.md, operator sessions).

- The session token is 32 random bytes, base64url without padding (43 chars), sent only in the
  ``fh_session`` cookie. The database keeps ``SHA-256(raw bytes)`` as the session id, so a read of
  the ``sessions`` table cannot be replayed as a cookie.
- The CSRF token is ``HMAC-SHA256(key=raw session bytes, msg=CSRF_CONTEXT)``: bound to the session,
  recomputable from the cookie (``/me`` hands it out again) and useless without it. The database
  keeps only ``SHA-256(csrf token)``; comparisons are constant-time.
"""

import base64
import binascii
import hashlib
import hmac
import re
import secrets
from typing import Final

SESSION_TOKEN_BYTES: Final = 32
CSRF_CONTEXT: Final = b"financial-hub/csrf/v1"
_TOKEN_TEXT: Final = re.compile(r"[A-Za-z0-9_-]{43}")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def new_session_token() -> tuple[str, bytes]:
    """``(cookie value, raw bytes)`` of a fresh session token."""
    raw = secrets.token_bytes(SESSION_TOKEN_BYTES)
    return _b64(raw), raw


def parse_session_token(value: str | None) -> bytes | None:
    """Raw bytes of a well-formed cookie value, else ``None`` (never raises)."""
    if value is None or _TOKEN_TEXT.fullmatch(value) is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(value + "=")
    except binascii.Error, ValueError:  # pragma: no cover - the pattern already guarantees it
        return None
    # Reject non-canonical encodings (unused trailing bits) so one token has one spelling.
    return raw if len(raw) == SESSION_TOKEN_BYTES and _b64(raw) == value else None


def session_id(raw: bytes) -> bytes:
    return hashlib.sha256(raw).digest()


def csrf_token_for(raw: bytes) -> str:
    return _b64(hmac.new(raw, CSRF_CONTEXT, hashlib.sha256).digest())


def csrf_token_hash(csrf_token: str) -> bytes:
    return hashlib.sha256(csrf_token.encode("utf-8")).digest()


def csrf_matches(stored_hash: bytes, presented: str | None) -> bool:
    """Constant-time check of a presented ``X-CSRF-Token`` against the stored hash."""
    if not presented:
        return False
    return hmac.compare_digest(stored_hash, csrf_token_hash(presented))
