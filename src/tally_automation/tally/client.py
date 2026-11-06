"""Read-only TallyPrime client (XML over HTTP), used only by the backend on Computer A.

There is deliberately no method that imports, creates, alters or deletes
anything. Every request body is checked by ``assert_read_only`` immediately
before it is sent. Voucher import arrives in Phase 8 as a separate, explicitly
approved operation.

Network rules:

* The host must be loopback or a Tailscale address. A hostname (e.g. a
  MagicDNS name) is accepted only if *every* address it resolves to is.
* Proxies from the environment are ignored (``trust_env=False``), so traffic
  cannot be diverted outside the tailnet. Redirects are not followed.
* Responses are streamed and refused beyond ``TALLY_MAX_RESPONSE_MB``.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from dataclasses import dataclass

import httpx

from tally_automation.config import Settings
from tally_automation.network import TAILSCALE_IPV4, TAILSCALE_IPV6
from tally_automation.tally import masters as m
from tally_automation.tally.xmlio import (
    ParsedResponse, TallyResponseError, assert_read_only, decode, export_collection_request, parse_response,
)


class TallyConfigError(Exception):
    """TallyPrime is not configured, or configured with a disallowed address."""


class TallyConnectionError(Exception):
    """TallyPrime could not be reached or did not answer in time."""


def _allowed(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return address.is_loopback or address in TAILSCALE_IPV4 or address in TAILSCALE_IPV6


def validate_tally_host(host: str) -> None:
    host = host.strip().strip("[]")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
        except OSError as exc:
            raise TallyConfigError(f"TALLY_HOST '{host}' could not be resolved.") from exc
        addresses = {ipaddress.ip_address(info[4][0].split("%")[0]) for info in infos}
        if not addresses or not all(_allowed(a) for a in addresses):
            raise TallyConfigError(
                f"TALLY_HOST '{host}' resolves to an address outside loopback/Tailscale; refused."
            )
        return
    if not _allowed(address):
        raise TallyConfigError(f"TALLY_HOST '{host}' is neither loopback nor a Tailscale address; refused.")


@dataclass
class ServerStatus:
    running: bool
    message: str
    milliseconds: int


@dataclass
class ExportResult:
    parsed: ParsedResponse
    size_bytes: int
    milliseconds: int


class TallyClient:
    def __init__(
        self, host: str, port: int, *, timeout: float, export_timeout: float, max_bytes: int,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        validate_tally_host(host)
        self.host, self.port = host, port
        bracketed = f"[{host}]" if ":" in host else host
        self.base_url = f"http://{bracketed}:{port}/"
        self.timeout = timeout
        self.export_timeout = export_timeout
        self.max_bytes = max_bytes
        self._client = httpx.Client(
            transport=transport, trust_env=False, follow_redirects=False,
            headers={"Content-Type": "text/xml; charset=utf-8"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> TallyClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- transport -------------------------------------------------------------------

    def _send(self, method: str, body: str | None, timeout: float) -> tuple[bytes, str | None, int]:
        if body is not None:
            assert_read_only(body)   # last line of defence, right before the network
        started = time.perf_counter()
        try:
            with self._client.stream(
                method, self.base_url, content=body.encode("utf-8") if body is not None else None,
                timeout=httpx.Timeout(timeout, connect=min(timeout, self.timeout)),
            ) as response:
                if response.status_code != 200:
                    raise TallyConnectionError(f"TallyPrime answered HTTP {response.status_code}.")
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise TallyResponseError(
                            f"TallyPrime's response exceeded {self.max_bytes // 1_000_000} MB and was refused."
                        )
                    chunks.append(chunk)
                content_type = response.headers.get("content-type")
        except httpx.TimeoutException as exc:
            raise TallyConnectionError(f"TallyPrime at {self.host}:{self.port} did not answer within {timeout:g} s.") from exc
        except httpx.TransportError as exc:
            raise TallyConnectionError(
                f"Could not connect to TallyPrime at {self.host}:{self.port} ({type(exc).__name__})."
            ) from exc
        return b"".join(chunks), content_type, round((time.perf_counter() - started) * 1000)

    def server_status(self) -> ServerStatus:
        """GET / — TallyPrime answers '<RESPONSE>TallyPrime Server is Running</RESPONSE>'."""
        content, content_type, ms = self._send("GET", None, self.timeout)
        message = decode(content, content_type).strip()
        running = "is running" in message.lower()
        clean = message.replace("<RESPONSE>", "").replace("</RESPONSE>", "").strip()[:200]
        return ServerStatus(running, clean or "No answer text.", ms)

    def export(self, collection_type: str, fields: tuple[str, ...], company: str | None, *, timeout: float | None = None) -> ExportResult:
        body = export_collection_request(collection_type, fields, company)
        content, content_type, ms = self._send("POST", body, timeout or self.export_timeout)
        return ExportResult(parse_response(decode(content, content_type)), len(content), ms)

    # ---- masters -----------------------------------------------------------------------

    def companies(self) -> list[m.Company]:
        """Companies currently loaded (open) in TallyPrime."""
        result = self.export("Company", m.COMPANY_FIELDS, None, timeout=self.timeout)
        return m.parse_companies(result.parsed.root)

    def masters(self, company: str) -> tuple[m.Masters, int]:
        """Groups, ledgers and voucher types of one loaded company.

        Returns the masters and the number of control characters removed.
        Raises if Tally returns no groups or no voucher types: every company has
        predefined ones, so an empty answer means the company was not used.
        """
        removed = 0
        results = {}
        for kind, fields in (("Group", m.GROUP_FIELDS), ("Ledger", m.LEDGER_FIELDS),
                             ("VoucherType", m.VOUCHER_TYPE_FIELDS)):
            results[kind] = self.export(kind, fields, company)
            removed += results[kind].parsed.removed_characters
        masters = m.Masters(
            groups=m.parse_groups(results["Group"].parsed.root),
            ledgers=m.parse_ledgers(results["Ledger"].parsed.root),
            voucher_types=m.parse_voucher_types(results["VoucherType"].parsed.root),
        )
        if not masters.groups or not masters.voucher_types:
            raise TallyResponseError(
                f"TallyPrime returned no groups or voucher types for '{company}'. "
                "It may have been closed in TallyPrime; load it and try again."
            )
        return masters, removed


def create_tally_client(settings: Settings, transport: httpx.BaseTransport | None = None) -> TallyClient:
    if not settings.tally_host:
        raise TallyConfigError("TallyPrime is not configured (TALLY_HOST is empty).")
    return TallyClient(
        settings.tally_host, settings.tally_port, timeout=settings.tally_timeout_seconds,
        export_timeout=settings.tally_export_timeout_seconds,
        max_bytes=settings.tally_max_response_mb * 1_000_000, transport=transport,
    )
