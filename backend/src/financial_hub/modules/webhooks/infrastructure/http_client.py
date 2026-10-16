"""SSRF-guarded HTTP client for outbound webhooks (docs/SECURITY.md, "Outbound webhooks").

For every send:

1. the URL is re-validated against the :class:`TargetPolicy` (https outside dev, no credentials);
2. the host is resolved (or parsed, for IP literals in any spelling) and *every* address is
   checked: one private, loopback, link-local, CGNAT, multicast, reserved or unspecified address
   (IPv4-mapped/compatible and NAT64/6to4/Teredo forms included) refuses the whole target;
3. the connection goes to the first checked address (pinned: no second lookup, so DNS rebinding
   cannot swap in an internal address), while the ``Host`` header, TLS SNI and certificate check
   keep the original name;
4. redirects are never followed, proxies from the environment are ignored, the whole exchange is
   bounded by one timeout and at most ``max_response_bytes`` of the response are read.
"""

import asyncio
import ipaddress
import socket
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

import httpx

from financial_hub.modules.webhooks.domain import (
    InvalidWebhookUrlError,
    IPAddress,
    TargetPolicy,
    TargetRefusedError,
    check_addresses,
    parse_ip_host,
    validate_url,
)

DEFAULT_TIMEOUT_SECONDS: Final = 10.0
DEFAULT_MAX_RESPONSE_BYTES: Final = 64 * 1024
USER_AGENT: Final = "FinancialHub-Webhooks/1"

type Resolver = Callable[[str, int], Awaitable[list[IPAddress]]]


class SendOutcome(StrEnum):
    DELIVERED = "delivered"
    FAILED = "failed"
    REFUSED = "refused"


@dataclass(frozen=True, slots=True)
class SendResult:
    outcome: SendOutcome
    status_code: int | None
    latency_ms: int
    error: str | None

    @property
    def delivered(self) -> bool:
        return self.outcome is SendOutcome.DELIVERED


async def system_resolver(host: str, port: int) -> list[IPAddress]:
    """All addresses ``host`` resolves to (A and AAAA), de-duplicated, in resolver order."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses: dict[IPAddress, None] = {}
    for info in infos:
        raw = str(info[4][0]).split("%", 1)[0]
        addresses[ipaddress.ip_address(raw)] = None
    return list(addresses)


class GuardedSender:
    def __init__(
        self,
        policy: TargetPolicy,
        *,
        resolver: Resolver = system_resolver,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._policy = policy
        self._resolver = resolver
        self._timeout = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._transport = transport

    async def resolve_target(self, url: str) -> tuple[httpx.URL, dict[str, str], dict[str, str]]:
        """The pinned URL plus the ``Host`` header and request extensions to send it with."""
        try:
            parts = validate_url(url, self._policy)
        except InvalidWebhookUrlError as exc:
            raise TargetRefusedError(str(exc)) from None
        host = parts.hostname or ""
        default_port = 443 if parts.scheme == "https" else 80
        port = parts.port or default_port
        literal = parse_ip_host(host)
        if literal is not None:
            addresses = [literal]
        else:
            try:
                addresses = await self._resolver(host, port)
            except (OSError, UnicodeError) as exc:
                raise TargetRefusedError(f"{host} could not be resolved ({exc})") from None
        if self._policy.is_allowlisted(host):
            if not addresses:
                raise TargetRefusedError(f"{host} did not resolve to any address")
        else:
            check_addresses(host, addresses)
        pinned = addresses[0]
        target = httpx.URL(url)
        pinned_url = target.copy_with(host=str(pinned))
        name = _url_host_name(host)
        host_header = name if port == default_port else f"{name}:{port}"
        extensions: dict[str, str] = {}
        if parts.scheme == "https" and literal is None:
            extensions["sni_hostname"] = host
        return pinned_url, {"Host": host_header}, extensions

    async def post(self, url: str, *, headers: Mapping[str, str], body: bytes) -> SendResult:
        started = time.monotonic()

        def elapsed() -> int:
            return max(int((time.monotonic() - started) * 1000), 0)

        try:
            async with asyncio.timeout(self._timeout):
                pinned_url, host_header, extensions = await self.resolve_target(url)
                return await self._exchange(
                    pinned_url, {**headers, **host_header}, body, extensions, elapsed
                )
        except TargetRefusedError as exc:
            return SendResult(SendOutcome.REFUSED, None, elapsed(), f"target refused: {exc}")
        except TimeoutError, httpx.TimeoutException:
            return SendResult(
                SendOutcome.FAILED, None, elapsed(), f"timeout after {self._timeout:g} s"
            )
        except httpx.HTTPError as exc:
            return SendResult(SendOutcome.FAILED, None, elapsed(), f"{type(exc).__name__}: {exc}")

    async def _exchange(
        self,
        url: httpx.URL,
        headers: Mapping[str, str],
        body: bytes,
        extensions: dict[str, str],
        elapsed: Callable[[], int],
    ) -> SendResult:
        transport = self._transport or httpx.AsyncHTTPTransport(retries=0)
        async with httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(self._timeout),
        ) as client:
            request = client.build_request(
                "POST",
                url,
                headers={"User-Agent": USER_AGENT, **headers},
                content=body,
                extensions=extensions,
            )
            response = await client.send(request, stream=True)
            try:
                # An in-memory response (test transports) is already read; a real one streams.
                if not response.is_stream_consumed:
                    read = 0
                    async for chunk in response.aiter_raw():
                        read += len(chunk)
                        if read >= self._max_response_bytes:
                            break
            finally:
                await response.aclose()
        status = response.status_code
        if 200 <= status < 300:
            return SendResult(SendOutcome.DELIVERED, status, elapsed(), None)
        if 300 <= status < 400:
            return SendResult(
                SendOutcome.FAILED, status, elapsed(), f"HTTP {status}: redirects are not followed"
            )
        return SendResult(SendOutcome.FAILED, status, elapsed(), f"HTTP {status}")


def _url_host_name(host: str) -> str:
    return f"[{host}]" if ":" in host else host
