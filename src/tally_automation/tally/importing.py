"""TallyPrime voucher import (Phase 8): the only write this application can make.

* ``assert_voucher_import`` – the guard every import body passes right before it
  is sent: an ``Import Data`` request to the ``Vouchers`` report containing
  exactly one ``VOUCHER`` with ``ACTION="Create"`` and nothing else (no
  masters, no alter/delete, no TDL).
* ``marker_lookup_request`` – an *export* (read) request for vouchers whose
  narration contains this application's marker, used before every import to
  refuse posting a voucher twice, and after it to verify what TallyPrime stored.
* ``parse_import_response`` – TallyPrime's counters (created, errors, …) and
  error lines.

Observed in TallyPrime 6.2 (read-only probe, 2026-11-06): an inline Voucher
collection with a ``$Narration Contains "…"`` filter works; with no match the
answer holds one empty ``VOUCHER`` element; the period comes from
``SVFROMDATE``/``SVTODATE``; ledger entries come back with ``UDF:`` tags.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from xml.sax.saxutils import escape

from tally_automation.tally.xmlio import (
    TallyResponseError, TallyWriteBlocked, assert_read_only, parse_response, text, unprefix,
)

MARKER = re.compile(r"^TA-[0-9A-F]{10}-(INV|T\d{1,6})$")
MARKER_PREFIX = re.compile(r"^TA-[0-9A-F]{10}-$")
MARKER_IN_TEXT = re.compile(r"\[(TA-[0-9A-F]{10}-(?:INV|T\d{1,6}))\]")
# Objects TallyPrime can create or change through an import; only VOUCHER is allowed.
_MASTER_TAGS = {
    "LEDGER", "GROUP", "STOCKITEM", "STOCKGROUP", "UNIT", "GODOWN", "COSTCENTRE", "COSTCATEGORY", "CURRENCY",
    "VOUCHERTYPE", "COMPANY", "BUDGET", "EMPLOYEE", "ATTENDANCETYPE", "TDLMESSAGE",
}


def assert_voucher_import(body: str) -> None:
    """Refuse anything but a single-voucher create import."""
    if body.count("<TALLYREQUEST>") != 1 or "<TALLYREQUEST>Import Data</TALLYREQUEST>" not in body:
        raise TallyWriteBlocked("Only a voucher 'Import Data' request may be sent as an import.")
    if re.search(r"<!(DOCTYPE|ENTITY)|<\s*TDL\b|ACTION\s*=\s*\"(?!Create\")", body, re.IGNORECASE):
        raise TallyWriteBlocked("The import contains a DTD, TDL or a non-create action and was not sent.")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise TallyWriteBlocked(f"The import XML is not well-formed ({exc}); not sent.") from exc
    if root.findtext("BODY/IMPORTDATA/REQUESTDESC/REPORTNAME") != "Vouchers":
        raise TallyWriteBlocked("The import is not addressed to the Vouchers report; not sent.")
    messages = root.findall("BODY/IMPORTDATA/REQUESTDATA/TALLYMESSAGE")
    children = [child for message in messages for child in message]
    if len(messages) != 1 or len(children) != 1 or children[0].tag != "VOUCHER":
        raise TallyWriteBlocked("The import must contain exactly one voucher and nothing else; not sent.")
    if children[0].get("ACTION") != "Create" or len(root.findall(".//VOUCHER")) != 1:
        raise TallyWriteBlocked("Only creating one new voucher is allowed; not sent.")
    if any(element.tag in _MASTER_TAGS for element in root.iter()):
        raise TallyWriteBlocked("The import contains master data; not sent.")
    if any(element.get("ACTION") not in (None, "Create") for element in root.iter()):
        raise TallyWriteBlocked("The import contains an alter or delete action; not sent.")


# ---- New ledger (the only master this application may create) ----------------------------------

_LEDGER_CHILDREN = {"NAME.LIST", "PARENT", "ISBILLWISEON"}


def ledger_create_request(company: str, name: str, parent: str) -> str:
    """Create ONE new ledger with just a name and a group. Nothing else is set; existing ledgers are never
    altered (TallyPrime refuses a create for a name that exists)."""
    body = (
        "<ENVELOPE><HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER><BODY><IMPORTDATA>"
        "<REQUESTDESC><REPORTNAME>All Masters</REPORTNAME>"
        f"<STATICVARIABLES><SVCURRENTCOMPANY>{escape(company)}</SVCURRENTCOMPANY></STATICVARIABLES></REQUESTDESC>"
        '<REQUESTDATA><TALLYMESSAGE xmlns:UDF="TallyUDF">'
        f'<LEDGER NAME="{escape(name, {chr(34): "&quot;"})}" ACTION="Create">'
        f"<NAME.LIST><NAME>{escape(name)}</NAME></NAME.LIST><PARENT>{escape(parent)}</PARENT>"
        "<ISBILLWISEON>No</ISBILLWISEON></LEDGER>"
        "</TALLYMESSAGE></REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>"
    )
    assert_ledger_create(body)
    return body


def assert_ledger_create(body: str) -> None:
    """Refuse anything but creating exactly one ledger with only a name, a group and bill-wise off."""
    if body.count("<TALLYREQUEST>") != 1 or "<TALLYREQUEST>Import Data</TALLYREQUEST>" not in body:
        raise TallyWriteBlocked("Only an 'Import Data' request may create a ledger.")
    if re.search(r"<!(DOCTYPE|ENTITY)|<\s*TDL\b", body, re.IGNORECASE):
        raise TallyWriteBlocked("The ledger import contains a DTD or TDL and was not sent.")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise TallyWriteBlocked(f"The ledger import XML is not well-formed ({exc}); not sent.") from exc
    if root.findtext("BODY/IMPORTDATA/REQUESTDESC/REPORTNAME") != "All Masters":
        raise TallyWriteBlocked("The ledger import is not addressed to the All Masters report; not sent.")
    messages = root.findall("BODY/IMPORTDATA/REQUESTDATA/TALLYMESSAGE")
    children = [child for message in messages for child in message]
    if len(messages) != 1 or len(children) != 1 or children[0].tag != "LEDGER":
        raise TallyWriteBlocked("The ledger import must contain exactly one ledger and nothing else; not sent.")
    ledger = children[0]
    if ledger.get("ACTION") != "Create" or any(e.get("ACTION") not in (None, "Create") for e in root.iter()):
        raise TallyWriteBlocked("Only creating a new ledger is allowed; not sent.")
    if {child.tag for child in ledger} - _LEDGER_CHILDREN or len(root.findall(".//LEDGER")) != 1:
        raise TallyWriteBlocked("The new ledger may only have a name, a group and bill-wise off; not sent.")
    names = [n.text or "" for n in ledger.findall("NAME.LIST/NAME")]
    if len(names) != 1 or names[0] != ledger.get("NAME") or not (ledger.findtext("PARENT") or "").strip():
        raise TallyWriteBlocked("The new ledger needs exactly one name and a group; not sent.")


# ---- Marker lookup (read) -----------------------------------------------------------------

LOOKUP_FIELDS = ("Date", "VoucherTypeName", "Narration", "MasterID", "GUID", "Reference", "PartyLedgerName",
                 "IsDeleted", "IsCancelled")


def marker_lookup_request(company: str, needle: str, from_date: date, to_date: date, *, entries: bool) -> str:
    """Export vouchers in the period whose narration contains ``needle``: a marker, a document's marker
    prefix (``TA-<hash>-``), or ``[TA-`` for all of this application's vouchers."""
    if needle != "[TA-" and not MARKER.match(needle) and not MARKER_PREFIX.match(needle):
        raise ValueError("invalid marker")
    methods = "".join(f"<NATIVEMETHOD>{f}</NATIVEMETHOD>" for f in LOOKUP_FIELDS)
    if entries:
        methods += "<NATIVEMETHOD>AllLedgerEntries</NATIVEMETHOD>"
    body = (
        "<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE>"
        "<ID>AppMarkedVouchers</ID></HEADER><BODY><DESC><STATICVARIABLES>"
        "<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"
        f"<SVCURRENTCOMPANY>{escape(company)}</SVCURRENTCOMPANY>"
        f'<SVFROMDATE TYPE="Date">{from_date:%Y%m%d}</SVFROMDATE><SVTODATE TYPE="Date">{to_date:%Y%m%d}</SVTODATE>'
        '</STATICVARIABLES><TDL><TDLMESSAGE><COLLECTION NAME="AppMarkedVouchers" ISINITIALIZE="Yes">'
        f"<TYPE>Voucher</TYPE>{methods}<FILTER>AppMarkerFilter</FILTER></COLLECTION>"
        f'<SYSTEM TYPE="Formulae" NAME="AppMarkerFilter">$Narration Contains "{escape(needle)}"</SYSTEM>'
        "</TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>"
    )
    assert_read_only(body)
    return body


def period_vouchers_request(company: str, from_date: date, to_date: date) -> str:
    """Export every voucher of the period with its ledger entries (read only), to find bank transactions
    that are already entered in TallyPrime, by hand or by this application."""
    methods = "".join(f"<NATIVEMETHOD>{f}</NATIVEMETHOD>" for f in LOOKUP_FIELDS + ("VoucherNumber",))
    methods += "<NATIVEMETHOD>AllLedgerEntries</NATIVEMETHOD>"
    body = (
        "<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE>"
        "<ID>AppPeriodVouchers</ID></HEADER><BODY><DESC><STATICVARIABLES>"
        "<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"
        f"<SVCURRENTCOMPANY>{escape(company)}</SVCURRENTCOMPANY>"
        f'<SVFROMDATE TYPE="Date">{from_date:%Y%m%d}</SVFROMDATE><SVTODATE TYPE="Date">{to_date:%Y%m%d}</SVTODATE>'
        '</STATICVARIABLES><TDL><TDLMESSAGE><COLLECTION NAME="AppPeriodVouchers" ISINITIALIZE="Yes">'
        f"<TYPE>Voucher</TYPE>{methods}</COLLECTION></TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>"
    )
    assert_read_only(body)
    return body


@dataclass
class TallyEntry:
    ledger: str
    amount: Decimal          # TallyPrime's sign: debit negative


@dataclass
class TallyVoucher:
    guid: str
    master_id: str | None
    voucher_date: date | None
    voucher_type: str | None
    narration: str | None
    markers: list[str]
    deleted: bool
    entries: list[TallyEntry] = field(default_factory=list)
    number: str | None = None


def _date(value: str | None) -> date | None:
    try:
        return date(int(value[:4]), int(value[4:6]), int(value[6:8])) if value and len(value) >= 8 else None
    except ValueError:
        return None


def parse_marked_vouchers(xml: str) -> list[TallyVoucher]:
    parsed = parse_response(xml)
    out: list[TallyVoucher] = []
    for v in parsed.root.iter("VOUCHER"):
        guid = text(v, "GUID")
        if guid is None:          # the empty placeholder TallyPrime sends when nothing matches
            continue
        narration = text(v, "NARRATION")
        entries = []
        for e in v.findall("ALLLEDGERENTRIES.LIST"):
            try:
                entries.append(TallyEntry(text(e, "LEDGERNAME") or "", Decimal(text(e, "AMOUNT") or "0")))
            except InvalidOperation:
                entries.append(TallyEntry(text(e, "LEDGERNAME") or "", Decimal("NaN")))
        out.append(TallyVoucher(
            guid=guid, master_id=text(v, "MASTERID"), voucher_date=_date(text(v, "DATE")),
            voucher_type=text(v, "VOUCHERTYPENAME"), narration=narration,
            markers=MARKER_IN_TEXT.findall(narration or ""),
            deleted=(text(v, "ISDELETED") or "No").lower() == "yes" or (text(v, "ISCANCELLED") or "No").lower() == "yes",
            entries=entries, number=text(v, "VOUCHERNUMBER"),
        ))
    return out


# ---- Import response ----------------------------------------------------------------------------

@dataclass
class ImportResult:
    created: int
    altered: int
    errors: int
    ignored: int
    last_voucher_id: str | None
    messages: list[str]
    excerpt: str

    @property
    def succeeded(self) -> bool:
        return self.created == 1 and self.errors == 0 and self.altered == 0 and not self.messages


def parse_import_response(raw: str) -> ImportResult:
    """TallyPrime's answer to an import. Raises ``TallyResponseError`` if it cannot be understood."""
    if not raw.strip():
        raise TallyResponseError("TallyPrime returned an empty answer to the import.")
    if re.search(r"<!(DOCTYPE|ENTITY)", raw, re.IGNORECASE):
        raise TallyResponseError("TallyPrime's answer contained a DTD and was refused.")
    cleaned = re.sub("[\x00-\x08\x0b\x0c\x0e-\x1f]", "", re.sub(r"&#(?:[0-8]|1[124-9]|2\d|3[01]);", "", raw))
    try:
        root = ET.fromstring(unprefix(cleaned))
    except ET.ParseError as exc:
        raise TallyResponseError(f"TallyPrime's answer to the import is not well-formed XML ({exc}).") from exc

    def count(tag: str) -> int | None:
        values = [e.text for e in root.iter(tag) if e.text and e.text.strip().lstrip("-").isdigit()]
        return int(values[0]) if values else None

    created, errors = count("CREATED"), count("ERRORS")
    messages = [m for m in ((e.text or "").strip() for e in root.iter("LINEERROR")) if m]
    if created is None and errors is None:
        if root.tag == "RESPONSE" and (root.text or "").strip():
            messages.append((root.text or "").strip()[:300])
            return ImportResult(0, 0, 1, 0, None, messages, raw[:2000])
        raise TallyResponseError("TallyPrime's answer to the import has no result counters.")
    last = next((e.text.strip() for e in root.iter("LASTVCHID") if e.text and e.text.strip() not in ("", "0")), None)
    return ImportResult(
        created=created or 0, altered=count("ALTERED") or 0, errors=errors or 0, ignored=count("IGNORED") or 0,
        last_voucher_id=last, messages=messages, excerpt=raw[:2000],
    )
