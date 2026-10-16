"""Pure parts of request protection: client IP behind proxies, policies, headers, key hashing."""

import ipaddress
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from financial_hub.platform.http.protection.idempotency import VALID_KEY, request_hash
from financial_hub.platform.http.protection.rate_limit import (
    BucketPolicy,
    Decision,
    client_ip,
    is_exempt,
)
from financial_hub.platform.settings import Settings

DB_URL = "postgresql+psycopg://u:p@db:5432/x"
PROXIES = (ipaddress.ip_network("10.0.0.0/8"), ipaddress.ip_network("fd00::/8"))


def scope(peer: str | None, forwarded: str | None = None) -> dict[str, Any]:
    headers = [] if forwarded is None else [(b"x-forwarded-for", forwarded.encode())]
    return {
        "type": "http",
        "client": None if peer is None else (peer, 1234),
        "headers": headers,
    }


@pytest.mark.parametrize(
    ("peer", "forwarded", "proxies", "expected"),
    [
        ("203.0.113.9", None, (), "203.0.113.9"),
        # Not behind a trusted proxy: the header is the client's own claim and is ignored.
        ("203.0.113.9", "198.51.100.1", PROXIES, "203.0.113.9"),
        ("203.0.113.9", "198.51.100.1", (), "203.0.113.9"),
        ("10.1.2.3", None, PROXIES, "10.1.2.3"),
        ("10.1.2.3", "198.51.100.1", PROXIES, "198.51.100.1"),
        # Right-most untrusted hop wins; the left part is client-controlled.
        ("10.1.2.3", "1.1.1.1, 198.51.100.1, 10.9.9.9", PROXIES, "198.51.100.1"),
        ("10.1.2.3", "10.2.2.2, 10.3.3.3", PROXIES, "10.2.2.2"),
        ("10.1.2.3", "garbage, 198.51.100.1", PROXIES, "198.51.100.1"),
        ("10.1.2.3", "198.51.100.1, garbage", PROXIES, "10.1.2.3"),
        ("fd00::1", "2001:db8::7", PROXIES, "2001:db8::7"),
        (None, "198.51.100.1", PROXIES, "unknown"),
        ("not-an-ip", None, (), "unknown"),
    ],
)
def test_client_ip(
    peer: str | None, forwarded: str | None, proxies: tuple[Any, ...], expected: str
) -> None:
    assert client_ip(scope(peer, forwarded), proxies) == expected


@pytest.mark.parametrize(
    ("path", "exempt"),
    [
        ("/health/live", True),
        ("/health/ready", True),
        ("/v1/projects/prj_abc/stream", True),
        ("/v1/projects/prj_abc/stream/x", False),
        ("/v1/projects", False),
        ("/v1/auth/login", False),
    ],
)
def test_exempt_paths(path: str, exempt: bool) -> None:
    assert is_exempt(path) is exempt


@pytest.mark.parametrize(
    ("capacity", "rate"), [(0, Decimal(1)), (1, Decimal(0)), (1, Decimal("-0.5"))]
)
def test_a_bucket_policy_needs_capacity_and_a_positive_rate(capacity: int, rate: Decimal) -> None:
    with pytest.raises(ValueError, match="capacity"):
        BucketPolicy(capacity, rate)


def test_decision_headers() -> None:
    allowed = Decision(allowed=True, limit=10, remaining=4, retry_after_seconds=0)
    assert allowed.headers() == {"RateLimit-Limit": "10", "RateLimit-Remaining": "4"}
    denied = Decision(allowed=False, limit=10, remaining=0, retry_after_seconds=0)
    assert denied.headers()["Retry-After"] == "1"  # never 0 on a denial
    assert Decision(False, 10, 0, 7).headers()["Retry-After"] == "7"


def settings(**values: object) -> Settings:
    return Settings(_env_file=None, database_url=DB_URL, **values)  # pyright: ignore[reportCallIssue]


def test_trusted_proxies_must_be_networks() -> None:
    proxies = settings(rate_limit_trusted_proxies=["10.0.0.0/8", "::1"])
    assert proxies.rate_limit_trusted_proxies == ["10.0.0.0/8", "::1"]
    with pytest.raises(ValidationError):
        settings(rate_limit_trusted_proxies=["nope"])


def test_request_hash_covers_method_path_and_body() -> None:
    base = request_hash("POST", "/v1/projects", b"{}")
    assert len(base) == 32
    assert base == request_hash("post", "/v1/projects", b"{}")
    assert base != request_hash("PUT", "/v1/projects", b"{}")
    assert base != request_hash("POST", "/v1/projectz", b"{}")
    assert base != request_hash("POST", "/v1/projects", b"{ }")


@pytest.mark.parametrize(
    ("key", "valid"),
    [("a", True), ("~" * 255, True), ("", False), ("a b", False), ("x" * 256, False)],
)
def test_key_syntax(key: str, valid: bool) -> None:
    assert (VALID_KEY.fullmatch(key) is not None) is valid
