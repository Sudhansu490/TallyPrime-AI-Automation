"""TallyPrime XML handling and the read-only client. SYNTHETIC data, fake transport only."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from tally_fakes import BAD_GSTIN, BANK_ACCOUNT, COMPANY, PARTY_GSTIN, FakeTally
from tally_automation.tally.client import (
    TallyClient, TallyConfigError, TallyConnectionError, validate_tally_host,
)
from tally_automation.tally.masters import COMPANY_FIELDS, resolve_primary_groups
from tally_automation.tally.xmlio import (
    TallyResponseError, TallyWriteBlocked, assert_read_only, export_collection_request, parse_response,
)


def client(fake: FakeTally, **kwargs) -> TallyClient:
    return TallyClient("127.0.0.1", 9000, timeout=5, export_timeout=5, max_bytes=kwargs.pop("max_bytes", 10_000_000),
                       transport=fake.transport)


# ---- Read-only guard -----------------------------------------------------------------------

def test_requests_are_exports_with_escaped_company() -> None:
    body = export_collection_request("Ledger", ("Name", "Parent"), "A & B <Test>")
    assert body.count("<TALLYREQUEST>Export</TALLYREQUEST>") == 1
    assert "<SVCURRENTCOMPANY>A &amp; B &lt;Test&gt;</SVCURRENTCOMPANY>" in body
    with pytest.raises(ValueError):
        export_collection_request("Ledger", ("Name</NATIVEMETHOD><X>",), None)


@pytest.mark.parametrize("body", [
    "<ENVELOPE><HEADER><TALLYREQUEST>Import</TALLYREQUEST></HEADER></ENVELOPE>",
    "<ENVELOPE><HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER></ENVELOPE>",
    "<ENVELOPE><HEADER><TALLYREQUEST>Export</TALLYREQUEST></HEADER><BODY><IMPORTDATA/></BODY></ENVELOPE>",
    '<ENVELOPE><HEADER><TALLYREQUEST>Export</TALLYREQUEST></HEADER><VOUCHER ACTION="Create"/></ENVELOPE>',
    "<ENVELOPE><HEADER><TALLYREQUEST>Export</TALLYREQUEST><TALLYREQUEST>Import</TALLYREQUEST></HEADER></ENVELOPE>",
    "<ENVELOPE><TALLYMESSAGE/></ENVELOPE>",
])
def test_anything_but_a_plain_export_is_blocked(body: str) -> None:
    with pytest.raises(TallyWriteBlocked):
        assert_read_only(body)


def test_client_sends_only_exports() -> None:
    fake = FakeTally()
    with client(fake) as c:
        c.server_status()
        c.masters(COMPANY)
    posts = [r for r in fake.requests if r.method == "POST"]
    assert len(posts) == 3 and all(b"<TALLYREQUEST>Export</TALLYREQUEST>" in r.content for r in posts)
    assert not any(b"Import" in r.content or b"ACTION=" in r.content for r in posts)
    # No addresses, balances or contact details are ever requested.
    assert not any(word in r.content for r in posts for word in (b"Address", b"Mailing", b"Balance", b"Email", b"Phone"))


# ---- Response parsing ------------------------------------------------------------------------

def test_control_characters_are_removed_and_counted() -> None:
    parsed = parse_response("<ENVELOPE><HEADER><STATUS>1</STATUS></HEADER><X>a&#4;b\x02c&#10;</X></ENVELOPE>")
    assert parsed.root.findtext("X") == "abc\n" and parsed.removed_characters == 2


@pytest.mark.parametrize(("text", "message"), [
    ("", "empty response"),
    ("<RESPONSE>Unknown Request, cannot be processed</RESPONSE>", "Unknown Request"),
    ("<ENVELOPE><BODY><DATA><LINEERROR>Could not find Report 'X'!</LINEERROR></DATA></BODY></ENVELOPE>", "Could not find"),
    ("<ENVELOPE><HEADER><STATUS>0</STATUS></HEADER></ENVELOPE>", "status 0"),
    ("<ENVELOPE><unclosed></ENVELOPE>", "not well-formed"),
    ('<!DOCTYPE x [<!ENTITY a "aaaa">]><ENVELOPE>&a;</ENVELOPE>', "DTD"),
])
def test_error_responses(text: str, message: str) -> None:
    with pytest.raises(TallyResponseError, match=message):
        parse_response(text)


def test_masters_are_parsed_like_tallyprime_6_2_sends_them() -> None:
    with client(FakeTally()) as c:
        companies = c.companies()
        masters, removed = c.masters(COMPANY)
    assert [(x.name, x.state, str(x.books_from)) for x in companies] == [(COMPANY, "Maharashtra", "2025-04-01")]
    assert removed >= 4   # &#4; markers and the raw \x02
    warnings = resolve_primary_groups(masters)
    assert warnings == []
    groups = {g.name: g for g in masters.groups}
    assert groups["Current Liabilities"].parent is None            # "&#4; Primary" → primary group
    assert groups["Sundry Creditors"].aliases == ["Creditors"]
    assert groups["Duties & Taxes"].primary_group == "Current Liabilities"
    assert groups["Purchase Accounts"].is_revenue is True

    ledgers = {l.name: l for l in masters.ledgers}
    supplier = ledgers["Synthetic Supplier Pvt Ltd"]
    # The latest dated GST registration wins over the older one and over PARTYGSTIN.
    assert (supplier.gstin, supplier.gst_registration_type, supplier.state) == (PARTY_GSTIN, "Regular", "Maharashtra")
    assert supplier.primary_group == "Current Liabilities"
    assert ledgers["Synthetic Typo Vendor"].gstin == BAD_GSTIN
    assert "Synthetic Office Supplies" in ledgers                   # control character removed
    duty = ledgers["Input CGST 9%"]
    assert (duty.tax_type, duty.gst_duty_head, duty.tax_rate) == ("GST", "CGST", Decimal("9"))
    assert (ledgers["Synthetic Bank"].bank_account_number, ledgers["Synthetic Bank"].ifsc) == (BANK_ACCOUNT, "SYNB0001234")
    assert ledgers["Profit & Loss A/c"].parent is None

    types = {v.name: v for v in masters.voucher_types}
    assert types["Purchase"].parent == "Purchase" and types["Old Purchase"].is_active is False


def test_unknown_company_returns_no_data_and_is_an_error() -> None:
    with client(FakeTally()) as c, pytest.raises(TallyResponseError, match="no groups or voucher types"):
        c.masters("SYNTHETIC NOT LOADED")


# ---- Network rules -----------------------------------------------------------------------------

@pytest.mark.parametrize("host", ["127.0.0.1", "100.124.182.62", "::1", "fd7a:115c:a1e0::1"])
def test_loopback_and_tailscale_hosts_are_allowed(host: str) -> None:
    validate_tally_host(host)


@pytest.mark.parametrize("host", ["8.8.8.8", "192.168.1.37", "10.0.0.5", "0.0.0.0"])
def test_public_and_lan_hosts_are_refused(host: str) -> None:
    with pytest.raises(TallyConfigError):
        validate_tally_host(host)


def test_hostname_must_resolve_to_allowed_addresses(monkeypatch) -> None:
    import socket

    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("100.100.1.1", 0))])
    validate_tally_host("tally-host.example.ts.net")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("100.100.1.1", 0)),
                                                                (2, 1, 6, "", ("203.0.113.9", 0))])
    with pytest.raises(TallyConfigError):
        validate_tally_host("tally-host.example.ts.net")


@pytest.mark.parametrize(("fail", "message"), [
    ("connect", "Could not connect"), ("timeout", "did not answer within"), ("http500", "HTTP 500"),
])
def test_connection_failures(fail: str, message: str) -> None:
    with client(FakeTally(fail=fail)) as c, pytest.raises(TallyConnectionError, match=message):
        c.server_status()


def test_oversized_responses_are_refused() -> None:
    with client(FakeTally(), max_bytes=500) as c, pytest.raises(TallyResponseError, match="exceeded"):
        c.masters(COMPANY)


def test_environment_proxies_are_ignored(monkeypatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://203.0.113.1:3128")
    fake = FakeTally()
    with client(fake) as c:
        assert c.server_status().running
    assert fake.requests[0].url.host == "127.0.0.1"
    assert COMPANY_FIELDS  # field list is fixed in code
    assert isinstance(fake.transport, httpx.MockTransport)
