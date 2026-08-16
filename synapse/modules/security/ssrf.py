"""SSRF protection for outbound web tools.

Blocks requests whose host resolves into loopback / private / link-local
space (including the cloud metadata range 169.254.169.254) and non-HTTP(S)
schemes, before any socket is opened. Callers apply the same check to every
redirect hop, so a public URL that bounces to an internal address is
rejected mid-chain.

ponytail: host is resolved once at check time — a DNS record that flips
between check and connect (rebinding) is out of scope; pinning the resolved
IP into the transport would be the upgrade path.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

_PRIVATE_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16",
    "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10",
    "::1/128", "fc00::/7", "fe80::/10", "::ffff:0:0/96",
))


def _is_private_ip(ip_str: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return any(addr in net for net in _PRIVATE_NETWORKS)


def _is_numeric_host_private(host: str) -> bool | None:
    """Handle non-dotted IP encodings some libc resolvers accept.

    `http://2130706433/` is 127.0.0.1 on glibc — Windows getaddrinfo rejects
    it instead, so check the numeric forms explicitly.
    """
    try:
        if host.isdigit():
            return _is_private_addr(ipaddress.ip_address(int(host)))
        if host.lower().startswith("0x"):
            return _is_private_addr(ipaddress.ip_address(int(host, 16)))
    except ValueError:
        pass
    return None


def _is_private_addr(addr) -> bool:
    return any(addr in net for net in _PRIVATE_NETWORKS)


def is_private_host(host: str | None) -> bool:
    """True when *host* is (or resolves to) a private/loopback address.

    Unresolvable hosts return False — the HTTP client then surfaces the real
    DNS error instead of a misleading SSRF message.
    """
    if not host:
        return False
    host = host.strip("[]")
    if _is_private_ip(host):
        return True
    numeric = _is_numeric_host_private(host)
    if numeric is not None:
        return numeric
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, OSError):
        return False
    return any(_is_private_ip(info[4][0]) for info in infos)


def check_url(url: str) -> str | None:
    """Return a rejection reason when *url* is SSRF-unsafe, else None."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return f"unparseable URL: {url!r}"
    if parsed.scheme not in ("http", "https"):
        return f"scheme '{parsed.scheme}' is not allowed (http/https only)"
    if not parsed.hostname:
        return "URL has no host"
    if is_private_host(parsed.hostname):
        return (f"host '{parsed.hostname}' resolves to a private, loopback or "
                f"link-local address (SSRF guard)")
    return None
