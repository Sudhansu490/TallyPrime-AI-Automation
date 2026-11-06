"""TallyPrime voucher import XML: rendering and structural verification.

The XML is *generated and stored only*. This module has no network code, and
the TallyPrime client refuses to send any body like this one (it is an
``Import`` request): importing is a separate operation that needs approval
(Phase 8).

TallyPrime's sign convention for ledger entries: a debit is
``ISDEEMEDPOSITIVE=Yes`` with a negative ``AMOUNT``; a credit is
``ISDEEMEDPOSITIVE=No`` with a positive ``AMOUNT``. A balanced voucher's
amounts sum to zero.

``verify_xml`` parses the rendered text back and checks it independently of
the code that wrote it: one voucher, the right company and voucher type,
well-formed dates and amounts, signs that agree with the debit/credit flags,
bill allocations that match their entries, a zero sum, and exactly the
entries of the draft.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from xml.sax.saxutils import escape, quoteattr

from tally_automation.validation.accounting import Check, Severity
from tally_automation.vouchers.drafts import ZERO, Draft, check, money

VCHTYPE_BY_KIND_LABEL = "Accounting Voucher View"
_AMOUNT = re.compile(r"^-?\d+\.\d{2}$")


def _date(value: date) -> str:
    return value.strftime("%Y%m%d")


def _tag(name: str, value: str | None) -> str:
    return f"<{name}>{escape(value)}</{name}>" if value else ""


def _signed(amount: Decimal, debit: bool) -> str:
    return money(-amount if debit else amount)


def render_xml(draft: Draft, company: str) -> str:
    entries = []
    for e in draft.entries:
        bill = ""
        if e.bill is not None:
            bill = (
                "<BILLALLOCATIONS.LIST>"
                + _tag("NAME", e.bill.name)
                + f"<BILLTYPE>{escape(e.bill.bill_type)}</BILLTYPE>"
                + f"<AMOUNT>{_signed(e.bill.amount, e.debit)}</AMOUNT>"
                + "</BILLALLOCATIONS.LIST>"
            )
        entries.append(
            "   <ALLLEDGERENTRIES.LIST>"
            f"<LEDGERNAME>{escape(e.ledger.name)}</LEDGERNAME>"
            f"<ISDEEMEDPOSITIVE>{'Yes' if e.debit else 'No'}</ISDEEMEDPOSITIVE>"
            f"<ISPARTYLEDGER>{'Yes' if draft.party is not None and e.ledger.guid == draft.party.guid else 'No'}</ISPARTYLEDGER>"
            f"<AMOUNT>{_signed(e.amount, e.debit)}</AMOUNT>"
            f"{bill}</ALLLEDGERENTRIES.LIST>\n"
        )
    party = draft.party.name if draft.party is not None else None
    voucher = (
        f"   <VOUCHER VCHTYPE={quoteattr(draft.voucher_type)} ACTION=\"Create\" OBJVIEW=\"{VCHTYPE_BY_KIND_LABEL}\">\n"
        f"    <DATE>{_date(draft.voucher_date)}</DATE>\n"
        f"    <VOUCHERTYPENAME>{escape(draft.voucher_type)}</VOUCHERTYPENAME>\n"
        + (f"    {_tag('VOUCHERNUMBER', draft.voucher_number)}\n" if draft.voucher_number else "")
        + (f"    {_tag('REFERENCE', draft.reference)}\n" if draft.reference else "")
        + (f"    <REFERENCEDATE>{_date(draft.reference_date)}</REFERENCEDATE>\n" if draft.reference_date else "")
        + (f"    {_tag('PARTYLEDGERNAME', party)}\n" if party else "")
        + (f"    {_tag('PARTYGSTIN', draft.party_gstin)}\n" if draft.party_gstin else "")
        + f"    {_tag('NARRATION', draft.narration)}\n"
        f"    <PERSISTEDVIEW>{VCHTYPE_BY_KIND_LABEL}</PERSISTEDVIEW>\n"
        "    <ISINVOICE>No</ISINVOICE>\n"
        + "".join(entries)
        + "   </VOUCHER>\n"
    )
    return (
        "<ENVELOPE>\n"
        " <HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>\n"
        " <BODY>\n  <IMPORTDATA>\n"
        "   <REQUESTDESC><REPORTNAME>Vouchers</REPORTNAME>"
        f"<STATICVARIABLES><SVCURRENTCOMPANY>{escape(company)}</SVCURRENTCOMPANY></STATICVARIABLES></REQUESTDESC>\n"
        "   <REQUESTDATA>\n    <TALLYMESSAGE xmlns:UDF=\"TallyUDF\">\n"
        + voucher
        + "    </TALLYMESSAGE>\n   </REQUESTDATA>\n  </IMPORTDATA>\n </BODY>\n</ENVELOPE>\n"
    )


# ---- Verification -------------------------------------------------------------------------------

def _fail(code: str, message: str) -> Check:
    return check(code, Severity.ERROR, False, message)


def _parse_amount(text: str | None) -> Decimal | None:
    if text is None or not _AMOUNT.match(text.strip()):
        return None
    try:
        return Decimal(text.strip())
    except InvalidOperation:
        return None


def verify_xml(xml: str, draft: Draft, company: str) -> list[Check]:
    """Structural checks of the rendered XML against the draft. An empty problem list means it passed."""
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", xml):
        return [_fail("xml_control_characters", "The XML contains control characters TallyPrime cannot read.")]
    if "<!DOCTYPE" in xml or "<!ENTITY" in xml:
        return [_fail("xml_dtd", "The XML contains a DTD.")]
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        return [_fail("xml_well_formed", f"The XML is not well-formed: {exc}.")]

    problems: list[Check] = []
    if root.tag != "ENVELOPE" or root.findtext("HEADER/TALLYREQUEST") != "Import Data":
        problems.append(_fail("xml_envelope", "The XML is not a TallyPrime 'Import Data' envelope."))
    if root.findtext("BODY/IMPORTDATA/REQUESTDESC/REPORTNAME") != "Vouchers":
        problems.append(_fail("xml_report", "The import is not addressed to TallyPrime's 'Vouchers' report."))
    if root.findtext("BODY/IMPORTDATA/REQUESTDESC/STATICVARIABLES/SVCURRENTCOMPANY") != company:
        problems.append(_fail("xml_company", "The XML names a different company than the one chosen."))
    vouchers = root.findall("BODY/IMPORTDATA/REQUESTDATA/TALLYMESSAGE/VOUCHER")
    if len(vouchers) != 1 or len(root.findall(".//VOUCHER")) != 1:
        return problems + [_fail("xml_one_voucher", "The XML must contain exactly one voucher.")]
    v = vouchers[0]
    if v.get("ACTION") != "Create":
        problems.append(_fail("xml_action", "The voucher must be created, never altered or deleted."))
    if v.get("VCHTYPE") != draft.voucher_type or v.findtext("VOUCHERTYPENAME") != draft.voucher_type:
        problems.append(_fail("xml_voucher_type", "The voucher type in the XML does not match."))
    try:
        if datetime.strptime(v.findtext("DATE") or "", "%Y%m%d").date() != draft.voucher_date:
            problems.append(_fail("xml_date", "The voucher date in the XML does not match."))
    except ValueError:
        problems.append(_fail("xml_date", "The voucher date in the XML is not a YYYYMMDD date."))
    if v.findtext("NARRATION") != draft.narration or draft.marker not in (v.findtext("NARRATION") or ""):
        problems.append(_fail("xml_narration", "The narration (with the voucher's marker) does not match."))

    found: list[tuple[str, bool, Decimal]] = []
    total = ZERO
    for i, entry in enumerate(v.findall("ALLLEDGERENTRIES.LIST"), start=1):
        name = entry.findtext("LEDGERNAME")
        positive = entry.findtext("ISDEEMEDPOSITIVE")
        amount = _parse_amount(entry.findtext("AMOUNT"))
        if not name or positive not in ("Yes", "No") or amount is None or amount == ZERO:
            problems.append(_fail("xml_entry", f"Ledger entry {i} is incomplete or has an invalid amount."))
            continue
        debit = positive == "Yes"
        if debit != (amount < ZERO):
            problems.append(_fail("xml_entry_sign", f"Ledger entry {i}: the amount's sign contradicts debit/credit."))
        bills = entry.findall("BILLALLOCATIONS.LIST")
        if bills:
            bill_total = sum((_parse_amount(b.findtext("AMOUNT")) or ZERO for b in bills), ZERO)
            if bill_total != amount or any(b.findtext("BILLTYPE") not in ("New Ref", "On Account", "Agst Ref") for b in bills):
                problems.append(_fail("xml_bill_allocation", f"Ledger entry {i}: bill allocations do not match the entry."))
        total += amount
        found.append((name, debit, abs(amount)))
    if len(found) < 2:
        problems.append(_fail("xml_entries", "A voucher needs at least two ledger entries."))
    if total != ZERO:
        problems.append(_fail("xml_balanced", f"The XML's ledger amounts do not sum to zero (difference {money(total)})."))
    expected = [(e.ledger.name, e.debit, e.amount) for e in draft.entries]
    if found != expected:
        problems.append(_fail("xml_matches_draft", "The XML's ledger entries differ from the voucher's entries."))
    return problems
