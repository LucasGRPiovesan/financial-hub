"""SSRF policy for outbound webhooks (docs/SECURITY.md, "Outbound webhooks" row).

A target is acceptable only when *every* address its name resolves to is publicly routable. The
classifier unwraps IPv6 forms that embed an IPv4 address (IPv4-mapped ``::ffff:a.b.c.d``,
IPv4-compatible ``::a.b.c.d``, NAT64 ``64:ff9b::/96``, 6to4 ``2002::/16`` and Teredo) and judges
the embedded address too. Host spellings ``inet_aton`` accepts (``2130706433``, ``0177.0.0.1``,
``0x7f.1``) are normalised to the address they denote, so they cannot slip past as "names".
"""

import ipaddress
import re
from typing import Final

type IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

_CGNAT: Final = ipaddress.IPv4Network("100.64.0.0/10")
_IPV6_ULA: Final = ipaddress.IPv6Network("fc00::/7")
_IPV6_SITE_LOCAL: Final = ipaddress.IPv6Network("fec0::/10")
_IPV4_COMPATIBLE: Final = ipaddress.IPv6Network("::/96")
_NAT64: Final = ipaddress.IPv6Network("64:ff9b::/96")
_NAT64_LOCAL: Final = ipaddress.IPv6Network("64:ff9b:1::/48")

_NUMERIC_PART: Final = re.compile(r"^(0[xX][0-9a-fA-F]*|0[0-7]*|[1-9][0-9]*)$")


class TargetRefusedError(Exception):
    """The webhook target is not allowed (private, reserved or unresolvable address)."""


def _numeric_part(part: str) -> int:
    if part[:2].lower() == "0x":
        return int(part[2:] or "0", 16)
    if len(part) > 1 and part.startswith("0"):
        return int(part, 8)
    return int(part)


def parse_ip_host(host: str) -> IPAddress | None:
    """The address ``host`` denotes when it is an IP literal in any spelling, else ``None``.

    Accepts ``[v6]`` and bare IPv6, dotted-quad IPv4 and the ``inet_aton`` forms: 1 to 4 parts,
    each decimal, octal (leading ``0``) or hex (``0x``), the last part filling the low bytes.
    """
    candidate = host.strip()
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    if ":" in candidate:
        try:
            return ipaddress.IPv6Address(candidate.split("%", 1)[0])
        except ValueError:
            return None
    candidate = candidate.removesuffix(".")
    parts = candidate.split(".")
    if not 1 <= len(parts) <= 4 or not all(_NUMERIC_PART.fullmatch(p) for p in parts):
        return None
    *head, last = [_numeric_part(part) for part in parts]
    if any(v > 0xFF for v in head) or last >= 1 << (8 * (4 - len(head))):
        return None
    number = 0
    for value in head:
        number = (number << 8) | value
    number = (number << (8 * (4 - len(head)))) | last
    return ipaddress.IPv4Address(number)


def _embedded_ipv4(address: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if address.sixtofour is not None:
        return address.sixtofour
    if address.teredo is not None:
        return address.teredo[1]
    if address in _NAT64 or address in _NAT64_LOCAL:
        return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    if address in _IPV4_COMPATIBLE and int(address) > 1:
        return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return None


def refusal_reason(address: IPAddress) -> str | None:
    """Why ``address`` may not be a webhook target, or ``None`` when it is public."""
    if isinstance(address, ipaddress.IPv6Address):
        embedded = _embedded_ipv4(address)
        if embedded is not None:
            reason = refusal_reason(embedded)
            if reason is not None:
                return f"embeds {embedded} ({reason})"
        if address in _IPV6_ULA:
            return "unique local address"
        if address in _IPV6_SITE_LOCAL:
            return "site-local address"
    elif address in _CGNAT:
        return "carrier-grade NAT address"
    checks = (
        (address.is_unspecified, "unspecified address"),
        (address.is_loopback, "loopback address"),
        (address.is_link_local, "link-local address"),
        (address.is_multicast, "multicast address"),
        (address.is_private, "private address"),
        (address.is_reserved, "reserved address"),
    )
    for flagged, reason in checks:
        if flagged:
            return reason
    if not address.is_global:
        return "not a globally routable address"
    return None


def check_addresses(host: str, addresses: list[IPAddress]) -> None:
    """Raise unless ``addresses`` is non-empty and every one of them is acceptable."""
    if not addresses:
        raise TargetRefusedError(f"{host} did not resolve to any address")
    for address in addresses:
        reason = refusal_reason(address)
        if reason is not None:
            raise TargetRefusedError(f"{host} resolves to {address}: {reason}")
