"""Vendor-neutral API normalization utilities."""

from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import urlparse

# Names that reach a model server on this machine (or the Docker host).
_LOCAL_HOSTS = frozenset({"localhost", "host.docker.internal"})

# Name suffixes that only resolve on this machine or the local network:
# RFC 6761 ``.localhost``, mDNS ``.local``, home routers' ``.lan`` and
# ``.home.arpa`` (RFC 8375), and ``.internal`` (cloud VPC / Docker names).
_LOCAL_SUFFIXES = (".localhost", ".local", ".lan", ".internal", ".home.arpa")

# Carrier-grade NAT (RFC 6598), used by Tailscale and some home networks.
# ``ipaddress`` does not count it as private.
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def is_local_host(host: str) -> bool:
    """Return True when *host* is on this machine or the local network.

    IP literals must be loopback, private (RFC 1918, IPv6 ULA), link-local or
    carrier-grade NAT addresses.  Names must be ``localhost``,
    ``host.docker.internal``, end in ``.localhost``, ``.local``, ``.lan``,
    ``.internal`` or ``.home.arpa``, or be single-label (a Docker Compose or
    Kubernetes service such as ``ollama``, a LAN machine such as ``gpu-box``:
    no public name has a single label), as the zero-cloud report counts them.
    No DNS lookups are made, so a public name that resolves to a LAN address
    is not local.
    """
    name = host.strip().strip("[]").rstrip(".").lower()
    if not name:
        return False
    try:
        ip = ipaddress.ip_address(name.split("%", 1)[0])  # drop an IPv6 zone id
    except ValueError:
        return name in _LOCAL_HOSTS or name.endswith(_LOCAL_SUFFIXES) or "." not in name
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or (isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT)
    )


def is_local_base_url(url: str) -> bool:
    """Return True when *url* points at a model server on this machine or the LAN.

    The parsed host is classified by :func:`is_local_host`
    (``http://localhost.example.com`` is not local).  URLs without a scheme
    (``localhost:11434/v1``) are accepted.
    """
    if not url:
        return False
    try:
        host = urlparse(url if "://" in url else f"//{url}").hostname or ""
    except ValueError:
        return False
    return is_local_host(host)


def normalize_messages(
    system_prompt: str,
    user_message: str,
) -> dict[str, Any]:
    """Create a vendor-neutral message structure.

    This is used internally to track what was sent to each provider,
    regardless of how each provider's API formats messages.
    """
    return {
        "system": system_prompt,
        "messages": [
            {"role": "user", "content": user_message},
        ],
    }
