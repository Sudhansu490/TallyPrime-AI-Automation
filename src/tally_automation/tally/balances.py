"""A ledger's balance on a date, read from TallyPrime (read only).

An inline Ledger collection limited to one ledger by name, exported with ``SVFROMDATE``/``SVTODATE``:
``ClosingBalance`` is the balance at the end of ``SVTODATE``. ``OpeningBalance`` is the ledger's balance at
the start of the books. TallyPrime signs debit balances negative (a bank account with money in it is a
debit balance), so the money in the bank account is ``-ClosingBalance``.

The request passes ``assert_read_only`` before it can be sent, like every export.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from xml.sax.saxutils import escape

from tally_automation.tally.xmlio import TallyResponseError, assert_read_only, parse_response, text


def ledger_balance_request(company: str, ledger: str, books_from: date, on: date) -> str:
    if '"' in ledger or "\n" in ledger:
        raise ValueError("ledger name cannot be used in a filter")
    body = (
        "<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE>"
        "<ID>AppLedgerBalance</ID></HEADER><BODY><DESC><STATICVARIABLES>"
        "<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"
        f"<SVCURRENTCOMPANY>{escape(company)}</SVCURRENTCOMPANY>"
        f'<SVFROMDATE TYPE="Date">{books_from:%Y%m%d}</SVFROMDATE><SVTODATE TYPE="Date">{on:%Y%m%d}</SVTODATE>'
        '</STATICVARIABLES><TDL><TDLMESSAGE><COLLECTION NAME="AppLedgerBalance" ISINITIALIZE="Yes">'
        "<TYPE>Ledger</TYPE><NATIVEMETHOD>Name</NATIVEMETHOD><NATIVEMETHOD>OpeningBalance</NATIVEMETHOD>"
        "<NATIVEMETHOD>ClosingBalance</NATIVEMETHOD><FILTER>AppLedgerIs</FILTER></COLLECTION>"
        f'<SYSTEM TYPE="Formulae" NAME="AppLedgerIs">$Name = "{escape(ledger)}"</SYSTEM>'
        "</TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>"
    )
    assert_read_only(body)
    return body


@dataclass(frozen=True)
class LedgerBalance:
    ledger: str
    on: date
    opening: Decimal | None      # at the start of the books, TallyPrime's sign
    closing: Decimal | None      # at the end of ``on``, TallyPrime's sign

    @property
    def bank_closing(self) -> Decimal | None:
        """Money in the bank account at the end of ``on`` (debit balance → positive)."""
        return None if self.closing is None else -self.closing

    @property
    def bank_opening(self) -> Decimal | None:
        return None if self.opening is None else -self.opening


def _amount(element, tag: str) -> Decimal | None:
    """TallyPrime sends a zero balance as an empty tag; a missing tag is unknown. Anything else must be a
    plain number."""
    found = element.find(tag)
    if found is None:
        return None
    value = (found.text or "").strip()
    if not value:
        return Decimal("0")
    try:
        return Decimal(value.replace(",", ""))
    except InvalidOperation:
        return None


def parse_ledger_balance(xml: str, ledger: str, on: date) -> LedgerBalance:
    parsed = parse_response(xml)
    wanted = " ".join(ledger.lower().split())
    for element in parsed.root.iter("LEDGER"):
        name = element.get("NAME") or text(element, "NAME") or ""
        if " ".join(name.lower().split()) == wanted:
            return LedgerBalance(ledger, on, _amount(element, "OPENINGBALANCE"), _amount(element, "CLOSINGBALANCE"))
    raise TallyResponseError(f"TallyPrime returned no ledger named '{ledger}'. Retrieve the masters again.")
