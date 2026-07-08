"""Shared SSRF guard (SSRF-1).

Used by every code path that makes a server-side HTTP request to a
user-submitted URL (manual "verify" endpoint, manual pipeline submission,
crawl/pipeline listing verification) to stop the container from being used
as a probe against internal-only LAN services or (if ever moved to cloud
infra) the cloud metadata endpoint.
"""
import ipaddress
import socket
from urllib.parse import urlparse


class UnsafeUrlError(ValueError):
    """Raised when a URL is not safe to fetch server-side."""


def _is_unsafe_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def assert_safe_external_url(url: str) -> None:
    """Raise UnsafeUrlError if *url* isn't a plain http(s) URL that resolves
    to a public address. Call this before any server-side fetch of a
    user-supplied URL."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeUrlError(f"Unsupported URL scheme: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise UnsafeUrlError("URL has no hostname")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"Could not resolve host: {host}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if _is_unsafe_ip(ip):
            raise UnsafeUrlError(f"{host} resolves to a non-public address ({ip})")
