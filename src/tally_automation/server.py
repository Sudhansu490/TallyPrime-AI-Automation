"""Production launcher: one Uvicorn server on explicit loopback/Tailscale sockets.

Uvicorn's ``--host`` accepts a single address, and ``0.0.0.0`` would expose the
app on every interface (including Wi-Fi/LAN). Instead we bind one socket per
configured address, after checking each address against a strict policy.

Run with: python -m tally_automation
"""

from __future__ import annotations

import ipaddress
import logging
import socket

import uvicorn

from tally_automation.api.main import create_app
from tally_automation.config import Settings, get_settings
from tally_automation.logging_config import configure_logging
from tally_automation.network import TAILSCALE_IPV4, TAILSCALE_IPV6

logger = logging.getLogger(__name__)



class BindHostError(ValueError):
    """Raised when a configured bind address would violate the network policy."""


def validate_bind_host(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Accept only loopback or Tailscale IP literals.

    Hostnames are rejected because they may resolve to a public interface.
    Wildcards (0.0.0.0, ::) are rejected because they bind every interface.
    """
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError as exc:
        raise BindHostError(f"bind host must be an IP address, got {host!r}") from exc
    if address.is_unspecified:
        raise BindHostError(f"wildcard bind address {host!r} is not allowed")
    if address.is_loopback or address in TAILSCALE_IPV4 or address in TAILSCALE_IPV6:
        return address
    raise BindHostError(f"{host!r} is neither loopback nor a Tailscale address")


def _bind_socket(address: ipaddress.IPv4Address | ipaddress.IPv6Address, port: int) -> socket.socket:
    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        # No SO_REUSEADDR: on Windows it would let another process hijack the port.
        sock.bind((str(address), port))
    except OSError:
        sock.close()
        raise
    sock.set_inheritable(True)
    return sock


def run(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_dir)
    addresses = [validate_bind_host(host) for host in settings.bind_hosts]

    sockets: list[socket.socket] = []
    try:
        for address in addresses:
            sockets.append(_bind_socket(address, settings.app_port))
    except OSError as exc:
        for sock in sockets:
            sock.close()
        logger.error(
            "could not bind listening socket",
            extra={"port": settings.app_port, "error": str(exc)},
        )
        raise SystemExit(1) from exc

    logger.info(
        "listening",
        extra={"addresses": [str(a) for a in addresses], "port": settings.app_port},
    )
    config = uvicorn.Config(
        create_app(settings),
        log_config=None,       # our JSON logging is already configured
        server_header=False,
        proxy_headers=False,   # no reverse proxy; never trust X-Forwarded-*
    )
    uvicorn.Server(config).run(sockets=sockets)
