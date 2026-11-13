"""TallyPrime client (XML over HTTP), used only by the backend on Computer A.

Reads are ``Export`` requests, each checked by ``assert_read_only`` immediately
before it is sent. The one write is ``import_voucher`` (Phase 8): it exists
only on a client created with ``write_enabled=True`` (``TALLY_WRITE_ENABLED``),
and its body must pass ``assert_voucher_import`` (exactly one new voucher,
nothing else) immediately before it is sent. Approval and duplicate checks
happen before the client is ever asked (``vouchers.importer``). The only other
write is ``create_ledger``, for a ledger the reviewer typed as new: it must pass
``assert_ledger_create`` (exactly one new ledger with a name, a group and
bill-wise off; never an alteration).

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
from tally_automation.tally.importing import (
    ImportResult, TallyVoucher, assert_ledger_create, assert_voucher_import, ledger_create_request,
    marker_lookup_request, parse_import_response, period_vouchers_request,
    parse_marked_vouchers,
)
from tally_automation.tally.xmlio import (
    ParsedResponse, TallyResponseError, TallyWriteBlocked, assert_read_only, decode, export_collection_request,
    parse_response,
)


class TallyConfigError(Exception):
    """TallyPrime is not configured, or configured with a disallowed address."""


class TallyConnectionError(Exception):
    """TallyPrime could not be reached or did not answer in time."""


class TallyNotSentError(TallyConnectionError):
    """The connection failed before the request could be sent: TallyPrime certainly did not receive it."""


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
        transport: httpx.BaseTransport | None = None, write_enabled: bool = False,
    ) -> None:
        validate_tally_host(host)
        self.write_enabled = write_enabled
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

    def _send(
        self, method: str, body: str | None, timeout: float, *, write: bool | str = False,
    ) -> tuple[bytes, str | None, int]:
        if body is not None:
            # Last line of defence, right before the network. ``write`` is True for a voucher import,
            # "ledger" for creating one new ledger; nothing else can be written.
            if write:
                if not self.write_enabled:
                    raise TallyWriteBlocked("Writing to TallyPrime is disabled (TALLY_WRITE_ENABLED=false).")
                if write == "ledger":
                    assert_ledger_create(body)
                else:
                    assert_voucher_import(body)
            else:
                assert_read_only(body)
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
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise TallyNotSentError(
                f"Could not connect to TallyPrime at {self.host}:{self.port} ({type(exc).__name__})."
            ) from exc
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


    # ---- vouchers (Phase 8) ---------------------------------------------------------------

    def period_vouchers(self, company: str, from_date, to_date) -> list[TallyVoucher]:
        """Every voucher of the period with its ledger entries (read-only)."""
        body = period_vouchers_request(company, from_date, to_date)
        content, content_type, _ = self._send("POST", body, self.export_timeout)
        return parse_marked_vouchers(decode(content, content_type))

    def marked_vouchers(
        self, company: str, needle: str, from_date, to_date, *, entries: bool = False,
    ) -> list[TallyVoucher]:
        """Vouchers in the period whose narration contains ``needle`` (read-only)."""
        body = marker_lookup_request(company, needle, from_date, to_date, entries=entries)
        content, content_type, _ = self._send("POST", body, self.export_timeout)
        return parse_marked_vouchers(decode(content, content_type))

    def ledger_names(self, company: str) -> set[str]:
        """Every ledger name in the company, lower-cased (read-only), to tell whether a new one exists."""
        result = self.export("Ledger", ("Name",), company)
        names = set()
        for ledger in result.parsed.root.iter("LEDGER"):
            name = ledger.get("NAME") or ledger.findtext("NAME") or ""
            if name.strip():
                names.add(" ".join(name.lower().split()))
        return names

    def create_ledger(self, company: str, name: str, parent: str) -> ImportResult:
        """Create ONE new ledger (name and group only). Raises ``TallyNotSentError`` only when it certainly
        was not sent."""
        body = ledger_create_request(company, name, parent)
        content, content_type, _ = self._send("POST", body, self.export_timeout, write="ledger")
        return parse_import_response(decode(content, content_type))

    def import_voucher(self, xml: str) -> ImportResult:
        """Send one approved voucher. Raises ``TallyNotSentError`` only when it certainly was not sent."""
        content, content_type, _ = self._send("POST", xml, self.export_timeout, write=True)
        return parse_import_response(decode(content, content_type))


def create_tally_client(settings: Settings, transport: httpx.BaseTransport | None = None) -> TallyClient:
    if not settings.tally_host:
        raise TallyConfigError("TallyPrime is not configured (TALLY_HOST is empty).")
    return TallyClient(
        settings.tally_host, settings.tally_port, timeout=settings.tally_timeout_seconds,
        export_timeout=settings.tally_export_timeout_seconds,
        max_bytes=settings.tally_max_response_mb * 1_000_000, transport=transport,
        write_enabled=settings.tally_write_enabled,
    )
