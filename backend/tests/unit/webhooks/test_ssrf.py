"""SSRF guard: address classification, IP spellings, URL rules and the guarded client."""

import ipaddress

import httpx
import pytest

from financial_hub.modules.webhooks.domain import (
    InvalidWebhookUrlError,
    IPAddress,
    TargetPolicy,
    TargetRefusedError,
    check_addresses,
    parse_ip_host,
    refusal_reason,
    validate_url,
)
from financial_hub.modules.webhooks.infrastructure.http_client import GuardedSender, SendOutcome

PRODUCTION = TargetPolicy()
DEV = TargetPolicy.for_environment(dev=True, allowed_hosts=frozenset({"receiver.test"}))


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "::1",
        "10.0.0.1",
        "172.16.5.4",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",  # noqa: S104 - an address under test, not a bind
        "224.0.0.1",
        "240.0.0.1",
        "255.255.255.255",
        "fd00::1",
        "fe80::1",
        "ff02::1",
        "::",
        "::ffff:127.0.0.1",
        "::ffff:169.254.169.254",
        "::127.0.0.1",
        "64:ff9b::a00:1",
        "2002:7f00:1::",
    ],
)
def test_non_public_addresses_are_refused(address: str) -> None:
    assert refusal_reason(ipaddress.ip_address(address)) is not None


@pytest.mark.parametrize("address", ["93.184.215.14", "8.8.8.8", "2606:4700:4700::1111"])
def test_public_addresses_are_accepted(address: str) -> None:
    assert refusal_reason(ipaddress.ip_address(address)) is None


@pytest.mark.parametrize(
    ("spelling", "address"),
    [
        ("2130706433", "127.0.0.1"),
        ("0177.0.0.1", "127.0.0.1"),
        ("0x7f.1", "127.0.0.1"),
        ("0x7f000001", "127.0.0.1"),
        ("127.1", "127.0.0.1"),
        ("10.1", "10.0.0.1"),
        ("017700000001", "127.0.0.1"),
        ("[::ffff:127.0.0.1]", "::ffff:127.0.0.1"),
        ("169.254.169.254.", "169.254.169.254"),
    ],
)
def test_inet_aton_spellings_are_normalised(spelling: str, address: str) -> None:
    parsed = parse_ip_host(spelling)

    assert parsed == ipaddress.ip_address(address)
    assert parsed is not None and refusal_reason(parsed) is not None


@pytest.mark.parametrize("host", ["example.com", "0xg", "1.2.3.4.5", "256.1.1.1.1", "08.1.1.1"])
def test_names_are_not_mistaken_for_literals(host: str) -> None:
    assert parse_ip_host(host) is None


def test_one_private_address_among_public_ones_refuses_the_target() -> None:
    addresses: list[IPAddress] = [
        ipaddress.ip_address("93.184.215.14"),
        ipaddress.ip_address("10.0.0.7"),
    ]

    with pytest.raises(TargetRefusedError, match=r"10\.0\.0\.7"):
        check_addresses("mixed.example", addresses)


def test_no_address_refuses_the_target() -> None:
    with pytest.raises(TargetRefusedError):
        check_addresses("empty.example", [])


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.example.com/x",  # http outside dev
        "ftp://hooks.example.com/x",
        "https://user:pw@hooks.example.com/x",
        "https://hooks.example.com/x#frag",
        "https:///nohost",
        "https://hooks.example.com:99999/x",
        "https://127.0.0.1/x",
        "https://2130706433/x",
        "https://0177.0.0.1/x",
        "https://[::1]/x",
        "https://[::ffff:127.0.0.1]/x",
        "https://169.254.169.254/latest/meta-data",
        "https://[fd00::1]/x",
        "https://localhost/x",
        "https://db.internal/x",
        "https://printer.local/x",
        "https://hooks.example.com/a b",
        "https://hooks.exämple.com/x",
    ],
)
def test_bad_urls_are_rejected_outside_dev(url: str) -> None:
    with pytest.raises(InvalidWebhookUrlError):
        validate_url(url, PRODUCTION)


def test_dev_allows_http_and_allowlisted_hosts_only() -> None:
    validate_url("http://receiver.test:8080/hook", DEV)
    validate_url("http://hooks.example.com/hook", DEV)
    with pytest.raises(InvalidWebhookUrlError):
        validate_url("http://127.0.0.1:8080/hook", DEV)


def test_allowlist_is_ignored_outside_dev() -> None:
    policy = TargetPolicy.for_environment(dev=False, allowed_hosts=frozenset({"127.0.0.1"}))

    assert policy == PRODUCTION
    with pytest.raises(InvalidWebhookUrlError):
        validate_url("https://127.0.0.1/hook", policy)


# --- the guarded client --------------------------------------------------------------------------


class FakeResolver:
    def __init__(self, *addresses: str) -> None:
        self.addresses: list[IPAddress] = [ipaddress.ip_address(a) for a in addresses]
        self.calls: list[str] = []

    async def __call__(self, host: str, port: int) -> list[IPAddress]:
        self.calls.append(f"{host}:{port}")
        return list(self.addresses)


class Recorder(httpx.MockTransport):
    def __init__(
        self, status: int = 200, body: bytes = b"ok", headers: dict[str, str] | None = None
    ) -> None:
        self.seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.seen.append(request)
            return httpx.Response(status, content=body, headers=headers)

        super().__init__(handler)


async def test_a_name_resolving_to_a_private_address_is_refused_before_connecting() -> None:
    transport = Recorder()
    sender = GuardedSender(PRODUCTION, resolver=FakeResolver("10.1.2.3"), transport=transport)

    result = await sender.post("https://rebind.example.com/hook", headers={}, body=b"{}")

    assert result.outcome is SendOutcome.REFUSED
    assert result.error is not None and "10.1.2.3" in result.error
    assert transport.seen == []


async def test_connects_to_the_pinned_address_with_the_original_host_and_sni() -> None:
    transport = Recorder()
    resolver = FakeResolver("93.184.215.14", "93.184.215.15")
    sender = GuardedSender(PRODUCTION, resolver=resolver, transport=transport)

    result = await sender.post("https://hooks.example.com/in?x=1", headers={"a": "b"}, body=b"{}")

    assert result.outcome is SendOutcome.DELIVERED and result.status_code == 200
    (request,) = transport.seen
    assert str(request.url) == "https://93.184.215.14/in?x=1"
    assert request.headers["host"] == "hooks.example.com"
    assert request.extensions["sni_hostname"] == "hooks.example.com"
    assert resolver.calls == ["hooks.example.com:443"]


async def test_ipv6_target_is_bracketed_and_port_kept_in_host() -> None:
    transport = Recorder()
    sender = GuardedSender(PRODUCTION, resolver=FakeResolver("2606:4700::1"), transport=transport)

    await sender.post("https://hooks.example.com:8443/in", headers={}, body=b"{}")

    (request,) = transport.seen
    assert str(request.url) == "https://[2606:4700::1]:8443/in"
    assert request.headers["host"] == "hooks.example.com:8443"


async def test_redirects_are_not_followed() -> None:
    transport = Recorder(302, headers={"Location": "http://169.254.169.254/"})
    sender = GuardedSender(PRODUCTION, resolver=FakeResolver("93.184.215.14"), transport=transport)

    result = await sender.post("https://hooks.example.com/in", headers={}, body=b"{}")

    assert result.outcome is SendOutcome.FAILED and result.status_code == 302
    assert result.error is not None and "redirect" in result.error
    assert len(transport.seen) == 1


async def test_non_2xx_is_a_failure_and_the_response_read_is_capped() -> None:
    transport = Recorder(500, body=b"x" * (1024 * 1024))
    sender = GuardedSender(
        PRODUCTION,
        resolver=FakeResolver("93.184.215.14"),
        transport=transport,
        max_response_bytes=1024,
    )

    result = await sender.post("https://hooks.example.com/in", headers={}, body=b"{}")

    assert result.outcome is SendOutcome.FAILED
    assert result.status_code == 500 and result.error == "HTTP 500"


async def test_unresolvable_name_is_refused() -> None:
    async def failing(host: str, port: int) -> list[IPAddress]:
        raise OSError("Name or service not known")

    sender = GuardedSender(PRODUCTION, resolver=failing, transport=Recorder())

    result = await sender.post("https://nowhere.example.com/in", headers={}, body=b"{}")

    assert result.outcome is SendOutcome.REFUSED


async def test_http_target_is_refused_outside_dev_even_when_public() -> None:
    sender = GuardedSender(PRODUCTION, resolver=FakeResolver("93.184.215.14"), transport=Recorder())

    result = await sender.post("http://hooks.example.com/in", headers={}, body=b"{}")

    assert result.outcome is SendOutcome.REFUSED
