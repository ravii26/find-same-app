# app/utils/ssrf_guard.py
"""SSRF guard — blocks requests to private/loopback/link-local/metadata IPs."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

_BLOCKED_RANGES: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    # IPv4 private / special
    ipaddress.IPv4Network("0.0.0.0/8"),
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("100.64.0.0/10"),
    ipaddress.IPv4Network("127.0.0.0/8"),
    ipaddress.IPv4Network("169.254.0.0/16"),    # link-local / cloud metadata
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.0.0.0/24"),
    ipaddress.IPv4Network("192.168.0.0/16"),
    ipaddress.IPv4Network("198.18.0.0/15"),
    ipaddress.IPv4Network("198.51.100.0/24"),
    ipaddress.IPv4Network("203.0.113.0/24"),
    ipaddress.IPv4Network("224.0.0.0/4"),       # multicast
    ipaddress.IPv4Network("240.0.0.0/4"),       # reserved
    # IPv6 special
    ipaddress.IPv6Network("::1/128"),            # loopback
    ipaddress.IPv6Network("::/128"),
    ipaddress.IPv6Network("fc00::/7"),           # unique local
    ipaddress.IPv6Network("fe80::/10"),          # link-local
    ipaddress.IPv6Network("ff00::/8"),           # multicast
    ipaddress.IPv6Network("::ffff:0:0/96"),      # IPv4-mapped
    # Cloud metadata endpoints
    ipaddress.IPv4Network("169.254.169.254/32"),
    ipaddress.IPv4Network("169.254.169.253/32"),
    ipaddress.IPv6Network("fd00:ec2::254/128"),
)


class SSRFError(ValueError):
    """Raised when a URL resolves to a blocked address."""


def _is_blocked(ip: str) -> bool:
    ip = ip.split("%", 1)[0]  # IPv6 zone id ("fe80::1%en0")
    try:
        try:
            addr: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.IPv4Address(ip)
        except ValueError:
            addr = ipaddress.IPv6Address(ip)
        return any(addr in net for net in _BLOCKED_RANGES)
    except ValueError:
        return True  # unparseable address → block


def check_url(url: str) -> str:
    """
    Validate a URL for SSRF safety.

    Resolves the hostname to all IP addresses and raises SSRFError if any
    resolve to a blocked range. Re-check after every redirect. Enforces
    http/https scheme only.

    Returns the original URL unchanged if safe.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise SSRFError(f"Scheme '{parsed.scheme}' is not allowed; use http or https")

    hostname = parsed.hostname
    if not hostname:
        raise SSRFError("URL has no hostname")

    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise SSRFError(f"DNS resolution failed for '{hostname}': {exc}") from exc

    for info in infos:
        ip = info[4][0]
        if _is_blocked(ip):
            raise SSRFError(f"URL '{url}' resolves to blocked address '{ip}'")

    return url


def check_ip(ip: str) -> None:
    """Raises SSRFError if ip is in a blocked range."""
    if _is_blocked(ip):
        raise SSRFError(f"IP address '{ip}' is in a blocked range")


async def resolve_safe(host: str, port: int) -> list[str]:
    """Resolve host once and return its addresses, raising SSRFError if ANY is blocked.

    Callers must connect to the returned addresses, not re-resolve the name: a second lookup can
    answer differently (DNS rebinding), turning a checked public address into a private one.
    Non-blocking (uses the event loop's resolver).
    """
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        pass
    else:
        check_ip(host)
        return [host]

    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SSRFError(f"DNS resolution failed for '{host}': {exc}") from exc
    ips = list(dict.fromkeys(info[4][0] for info in infos))
    if not ips:
        raise SSRFError(f"'{host}' resolved to no addresses")
    for ip in ips:
        if _is_blocked(ip):
            raise SSRFError(f"'{host}' resolves to blocked address '{ip}'")
    return ips
