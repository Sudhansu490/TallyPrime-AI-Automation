"""A fake TallyPrime server (httpx transport) answering with SYNTHETIC masters.

The XML mirrors what TallyPrime 6.2 actually sends: names in the NAME
attribute, aliases in LANGUAGENAME.LIST, ``&#4;`` markers, a raw control
character, dated GST registration entries, and STATUS 1 with no records for
an unknown company. No real company data.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

import httpx

COMPANY = "SYNTHETIC TRADERS (TEST)"
COMPANY_GUID = "00000000-0000-0000-0000-00000000c0de"
PARTY_GSTIN = "27AAAAA0000A1Z2"   # valid check character
BAD_GSTIN = "27AAAAA0000A1Z9"   # wrong check character
BANK_ACCOUNT = "123456789012"

STATUS_TEXT = "<RESPONSE>TallyPrime Server is Running</RESPONSE>"


def _envelope(items: str) -> str:
    return (
        "<ENVELOPE>\n <HEADER>\n  <VERSION>1</VERSION>\n  <STATUS>1</STATUS>\n </HEADER>\n <BODY>\n"
        "  <DESC><CMPINFO><COMPANY>0</COMPANY></CMPINFO></DESC>\n"
        f"  <DATA>\n   <COLLECTION>\n{items}   </COLLECTION>\n  </DATA>\n </BODY>\n</ENVELOPE>\n"
    )


def _names(name: str, *aliases: str) -> str:
    listed = "".join(f"<NAME>{n}</NAME>" for n in (name, *aliases))
    return f"<LANGUAGENAME.LIST><NAME.LIST TYPE=\"String\">{listed}</NAME.LIST><LANGUAGEID TYPE=\"Number\"> 1033</LANGUAGEID></LANGUAGENAME.LIST>"


COMPANIES = _envelope(f"""    <COMPANY NAME="{COMPANY.replace('&', '&amp;')}" RESERVEDNAME="">
     <STARTINGFROM TYPE="Date">20250401</STARTINGFROM>
     <BOOKSFROM TYPE="Date">20250401</BOOKSFROM>
     <NAME TYPE="String">{COMPANY}</NAME>
     <GUID TYPE="String">{COMPANY_GUID}</GUID>
     <COUNTRYNAME TYPE="String">India</COUNTRYNAME>
     <STATENAME TYPE="String">Maharashtra</STATENAME>
    </COMPANY>
""")


def _group(name: str, parent: str, revenue: str = "No", positive: str = "Yes", *aliases: str) -> str:
    return (f'    <GROUP NAME="{name}" RESERVEDNAME=""><GUID TYPE="String">g-{name}</GUID>'
            f'<PARENT TYPE="String">{parent}</PARENT><ISREVENUE TYPE="Logical">{revenue}</ISREVENUE>'
            f'<ISDEEMEDPOSITIVE TYPE="Logical">{positive}</ISDEEMEDPOSITIVE><ALTERID TYPE="Number"> 5</ALTERID>'
            f"{_names(name, *aliases)}</GROUP>\n")


GROUPS = _envelope(
    _group("Current Liabilities", "&#4; Primary", "No", "No")
    + _group("Sundry Creditors", "Current Liabilities", "No", "No", "Creditors")
    + _group("Duties &amp; Taxes", "Current Liabilities", "No", "No")
    + _group("Current Assets", "&#4; Primary")
    + _group("Bank Accounts", "Current Assets")
    + _group("Purchase Accounts", "&#4; Primary", "Yes", "Yes")
)


def _ledger(name: str, parent: str, extra: str = "", tax_type: str = "Others") -> str:
    return (f'    <LEDGER NAME="{name}" RESERVEDNAME=""><GUID TYPE="String">l-{name}</GUID>'
            f'<PARENT TYPE="String">{parent}</PARENT><TAXTYPE TYPE="String">{tax_type}</TAXTYPE>'
            f'<ISBILLWISEON TYPE="Logical">No</ISBILLWISEON><ALTERID TYPE="Number"> 7</ALTERID>{extra}'
            f"{_names(name)}</LEDGER>\n")


LEDGERS = _envelope(
    # Old PARTYGSTIN superseded by the latest dated registration entry.
    _ledger("Synthetic Supplier Pvt Ltd", "Sundry Creditors",
            "<PARTYGSTIN>27ZZZZZ9999Z1Z0</PARTYGSTIN>"
            "<LEDGSTREGDETAILS.LIST><APPLICABLEFROM>20170701</APPLICABLEFROM><GSTREGISTRATIONTYPE>Composition"
            "</GSTREGISTRATIONTYPE><GSTIN>27ZZZZZ9999Z1Z0</GSTIN></LEDGSTREGDETAILS.LIST>"
            "<LEDGSTREGDETAILS.LIST><APPLICABLEFROM>20240401</APPLICABLEFROM><GSTREGISTRATIONTYPE>Regular"
            f"</GSTREGISTRATIONTYPE><STATE>Maharashtra</STATE><GSTIN>{PARTY_GSTIN.lower()}</GSTIN>"
            "</LEDGSTREGDETAILS.LIST>")
    + _ledger("Synthetic Typo Vendor", "Sundry Creditors", f"<PARTYGSTIN>{BAD_GSTIN}</PARTYGSTIN>")
    # A raw control character inside a value, as TallyPrime really sends.
    + _ledger("Synthetic Office\x02 Supplies", "Purchase Accounts")
    + _ledger("Input CGST 9%", "Duties &amp; Taxes",
              "<GSTDUTYHEAD>CGST</GSTDUTYHEAD><RATEOFTAXCALCULATION> 9</RATEOFTAXCALCULATION>", tax_type="GST")
    + _ledger("Synthetic Bank", "Bank Accounts",
              f"<BANKDETAILS>{BANK_ACCOUNT}</BANKDETAILS><IFSCODE>SYNB0001234</IFSCODE>")
    + _ledger("Profit &amp; Loss A/c", "&#4; Primary")
)

VOUCHER_TYPES = _envelope(
    '    <VOUCHERTYPE NAME="Purchase" RESERVEDNAME=""><GUID>v-1</GUID><PARENT>Purchase</PARENT>'
    "<NUMBERINGMETHOD>Automatic</NUMBERINGMETHOD><ISACTIVE>Yes</ISACTIVE></VOUCHERTYPE>\n"
    '    <VOUCHERTYPE NAME="Payment" RESERVEDNAME=""><GUID>v-2</GUID><PARENT>Payment</PARENT>'
    "<NUMBERINGMETHOD>Automatic</NUMBERINGMETHOD><ISACTIVE>Yes</ISACTIVE></VOUCHERTYPE>\n"
    '    <VOUCHERTYPE NAME="Old Purchase" RESERVEDNAME=""><GUID>v-3</GUID><PARENT>Purchase</PARENT>'
    "<NUMBERINGMETHOD>Manual</NUMBERINGMETHOD><ISACTIVE>No</ISACTIVE></VOUCHERTYPE>\n"
)

EMPTY = _envelope("")


class FakeTally:
    """Records every request; answers like TallyPrime 6.2."""

    def __init__(self, *, loaded: bool = True, fail: str | None = None) -> None:
        self.loaded = loaded
        self.fail = fail            # "connect", "timeout" or "http500"
        self.requests: list[httpx.Request] = []
        self.overrides: dict[str, str] = {}
        # Phase 8: vouchers "in TallyPrime" (created by imports) and how imports are answered:
        # "ok", "refuse", "timeout_after_create" (created, but the answer is lost), "garbage".
        self.vouchers: list[dict] = []
        self.import_mode = "ok"
        self.imports = 0
        # New ledgers created through a ledger import: (name, parent); "refuse" makes them fail.
        self.created_ledgers: list[tuple[str, str]] = []
        self.ledger_mode = "ok"

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail == "connect":
            raise httpx.ConnectError("synthetic refused", request=request)
        if self.fail == "timeout":
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        if self.fail == "http500":
            return httpx.Response(500, text="error")
        if request.method == "GET":
            return httpx.Response(200, text=STATUS_TEXT, headers={"content-type": "text/xml; charset=utf-8"})
        body = request.content.decode()
        if "<TALLYREQUEST>Import Data</TALLYREQUEST>" in body and "<REPORTNAME>All Masters</REPORTNAME>" in body:
            return self._create_ledger(body)
        if "<TALLYREQUEST>Import Data</TALLYREQUEST>" in body:
            return self._import(request, body)
        collection = re.search(r'<COLLECTION NAME="App(\w+)"', body).group(1)
        if collection == "MarkedVouchers":
            return self._lookup(body)
        company = re.search(r"<SVCURRENTCOMPANY>(.*?)</SVCURRENTCOMPANY>", body)
        if collection in self.overrides:
            text = self.overrides[collection]
        elif collection == "Company":
            text = COMPANIES if self.loaded else EMPTY
        elif company is None or company.group(1).replace("&amp;", "&") != COMPANY or not self.loaded:
            text = EMPTY            # Tally's real behaviour: STATUS 1, no records
        else:
            text = {"Group": GROUPS, "Ledger": LEDGERS, "VoucherType": VOUCHER_TYPES}[collection]
        if collection == "Ledger" and self.created_ledgers and "</COLLECTION>" in text:
            created = "".join(_ledger(name, parent) for name, parent in self.created_ledgers)
            text = text.replace("   </COLLECTION>", created + "   </COLLECTION>")
        return httpx.Response(200, content=text.encode(), headers={"content-type": "text/xml; charset=utf-8"})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    # ---- Phase 8: imports and marker lookups (SYNTHETIC) --------------------------------------

    def _import(self, request: httpx.Request, body: str) -> httpx.Response:
        self.imports += 1
        if self.import_mode == "refuse":
            return httpx.Response(200, text=IMPORT_REFUSED)
        if self.import_mode == "garbage":
            return httpx.Response(200, text="<ENVELOPE><BODY>???</BODY></ENVELOPE>")
        voucher = ET.fromstring(body).find(".//VOUCHER")
        self.vouchers.append({
            "date": voucher.findtext("DATE"), "type": voucher.findtext("VOUCHERTYPENAME"),
            "narration": voucher.findtext("NARRATION"), "guid": f"synthetic-guid-{len(self.vouchers) + 1}",
            "master_id": str(1000 + len(self.vouchers)),
            "entries": [(e.findtext("LEDGERNAME"), e.findtext("AMOUNT")) for e in voucher.findall("ALLLEDGERENTRIES.LIST")],
        })
        if self.import_mode == "timeout_after_create":
            raise httpx.ReadTimeout("synthetic: created, answer lost", request=request)
        return httpx.Response(200, text=IMPORT_CREATED.format(id=self.vouchers[-1]["master_id"]))

    def _create_ledger(self, body: str) -> httpx.Response:
        ledger = ET.fromstring(body).find(".//LEDGER")
        name, parent = ledger.get("NAME"), ledger.findtext("PARENT")
        if self.ledger_mode == "refuse":
            return httpx.Response(200, text=IMPORT_REFUSED.replace("Ledger 'Synthetic Missing' does not exist!",
                                                                    f"Group '{parent}' does not exist!"))
        self.created_ledgers.append((name, parent))
        return httpx.Response(200, text=IMPORT_CREATED.format(id=0))

    def _lookup(self, body: str) -> httpx.Response:
        needle = re.search(r'\$Narration Contains "([^"]+)"', body).group(1)
        start = re.search(r"<SVFROMDATE[^>]*>(\d{8})<", body).group(1)
        end = re.search(r"<SVTODATE[^>]*>(\d{8})<", body).group(1)
        entries = "AllLedgerEntries" in body
        items = []
        for v in self.vouchers:
            if needle in (v["narration"] or "") and start <= v["date"] <= end:
                lines = "".join(
                    f"<ALLLEDGERENTRIES.LIST><LEDGERNAME>{escape(name)}</LEDGERNAME><AMOUNT>{amount}</AMOUNT>"
                    "<UDF:SYNTHETICFIELD.LIST DESC=\"x\"><UDF:X>1</UDF:X></UDF:SYNTHETICFIELD.LIST></ALLLEDGERENTRIES.LIST>"
                    for name, amount in v["entries"]) if entries else ""
                items.append(
                    f'<VOUCHER REMOTEID="{v["guid"]}" VCHTYPE="{escape(v["type"])}"><DATE>{v["date"]}</DATE>'
                    f'<GUID>{v["guid"]}</GUID><MASTERID> {v["master_id"]}</MASTERID>'
                    f'<VOUCHERTYPENAME>{escape(v["type"])}</VOUCHERTYPENAME><NARRATION>{escape(v["narration"])}</NARRATION>'
                    f"<ISDELETED>No</ISDELETED>{lines}</VOUCHER>\n")
        # With no match TallyPrime 6.2 still sends one empty VOUCHER element.
        return httpx.Response(200, text=_envelope("".join(items) or "<VOUCHER></VOUCHER>\n"),
                              headers={"content-type": "text/xml; charset=utf-8"})


IMPORT_CREATED = (
    "<ENVELOPE><HEADER><VERSION>1</VERSION><STATUS>1</STATUS></HEADER><BODY><DATA><IMPORTRESULT>"
    "<CREATED>1</CREATED><ALTERED>0</ALTERED><DELETED>0</DELETED><LASTVCHID>{id}</LASTVCHID><LASTMID>0</LASTMID>"
    "<COMBINED>0</COMBINED><IGNORED>0</IGNORED><ERRORS>0</ERRORS><CANCELLED>0</CANCELLED>"
    "</IMPORTRESULT></DATA></BODY></ENVELOPE>"
)
IMPORT_REFUSED = (
    "<ENVELOPE><HEADER><VERSION>1</VERSION><STATUS>1</STATUS></HEADER><BODY><DATA>"
    "<LINEERROR>Ledger 'Synthetic Missing' does not exist!</LINEERROR><IMPORTRESULT><CREATED>0</CREATED>"
    "<ALTERED>0</ALTERED><ERRORS>1</ERRORS></IMPORTRESULT></DATA></BODY></ENVELOPE>"
)
