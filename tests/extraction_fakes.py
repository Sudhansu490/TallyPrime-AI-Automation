"""SYNTHETIC extraction fixtures: document text and a fake LLM. No real data."""

from __future__ import annotations

import json
from typing import Any

from tally_automation.db.models import DocumentType
from tally_automation.extraction.llm import LlmError, LlmResponse
from tally_automation.extraction.parsing import gstin_checksum
from tally_automation.extraction.schemas import SCHEMAS
from tally_automation.extraction.service import PageInput

SUPPLIER_GSTIN = "27AAAAA0000A1Z" + gstin_checksum("27AAAAA0000A1Z")
BUYER_GSTIN = "29BBBBB1111B1Z" + gstin_checksum("29BBBBB1111B1Z")

INVOICE_TEXT = f"""SYNTHETIC TEST INVOICE - NOT A REAL DOCUMENT
TAX INVOICE
Supplier: Synthetic Supplies Pvt Ltd
GSTIN: {SUPPLIER_GSTIN}
Invoice No: SYN-0001 Date: 01/04/2026
Bill To: Synthetic Buyer LLP GSTIN: {BUYER_GSTIN}
Item HSN Qty Rate Taxable IGST 18% Total
Synthetic Widget 8471 10 100.00 1,000.00 180.00 1,180.00
Grand Total 1,180.00"""

STATEMENT_PAGE_1 = """SYNTHETIC BANK - TEST STATEMENT - NOT REAL
Account No: 123456789012 IFSC: SYNB0001234
Statement period 01/04/2026 to 30/04/2026
Opening Balance 10,000.00 Cr
02/04/2026 NEFT SYNTHETIC VENDOR UTR123 5,000.00 5,000.00 Cr"""

STATEMENT_PAGE_2 = """SYNTHETIC BANK - page 2
05/04/2026 SYNTHETIC SALARY CREDIT 2,500.00 7,500.00 Cr
Closing Balance 7,500.00 Cr"""


def field(value: str | None, evidence: str | None = None, page: int | None = 1, confidence: float = 0.97) -> dict:
    if value is None:
        return {"value": None, "evidence": None, "page": None, "confidence": 0.0}
    return {"value": value, "evidence": evidence if evidence is not None else value, "page": page, "confidence": confidence}


def reply(document_type: DocumentType, header: dict[str, dict], rows: list[dict], *,
          expected: bool = True, reason: str | None = None) -> str:
    schema = SCHEMAS[document_type]
    full_header = {spec.name: header.get(spec.name, field(None)) for spec in schema.header_fields}
    full_rows = [
        {"row_text": r.get("row_text"), "page": r.get("page", 1), "confidence": r.get("confidence", 0.95),
         **{spec.name: r.get(spec.name) for spec in schema.row_fields}}
        for r in rows
    ]
    return json.dumps({
        "document_check": {"is_expected_type": expected, "reason": reason},
        schema.header_key: full_header, schema.rows_key: full_rows,
    })


def invoice_reply(**overrides: Any) -> str:
    header = {
        "supplier_name": field("Synthetic Supplies Pvt Ltd", "Supplier: Synthetic Supplies Pvt Ltd"),
        "supplier_gstin": field(SUPPLIER_GSTIN, f"GSTIN: {SUPPLIER_GSTIN}"),
        "buyer_name": field("Synthetic Buyer LLP", "Bill To: Synthetic Buyer LLP"),
        "buyer_gstin": field(BUYER_GSTIN),
        "invoice_number": field("SYN-0001", "Invoice No: SYN-0001"),
        "invoice_date": field("01/04/2026", "Date: 01/04/2026"),
        "invoice_type": field("TAX INVOICE"),
        "total_igst": field(None),
        "grand_total": field("1,180.00", "Grand Total 1,180.00"),
    }
    header.update(overrides.pop("header", {}))
    rows = overrides.pop("rows", [{
        "row_text": "Synthetic Widget 8471 10 100.00 1,000.00 180.00 1,180.00",
        "description": "Synthetic Widget", "hsn_sac": "8471", "quantity": "10", "rate": "100.00",
        "taxable_amount": "1,000.00", "igst_rate": "18%", "igst_amount": "180.00", "line_total": "1,180.00",
    }])
    return reply(DocumentType.PURCHASE_INVOICE, header, rows, **overrides)


def statement_page1_reply() -> str:
    return reply(DocumentType.BANK_STATEMENT, {
        "bank_name": field("SYNTHETIC BANK", "SYNTHETIC BANK - TEST STATEMENT"),
        "account_number": field("123456789012", "Account No: 123456789012 IFSC: SYNB0001234"),
        # The IFSC quote contains the account number too: it must be masked there as well.
        "ifsc": field("SYNB0001234", "Account No: 123456789012 IFSC: SYNB0001234"),
        "period_from": field("01/04/2026", "Statement period 01/04/2026 to 30/04/2026"),
        "period_to": field("30/04/2026", "Statement period 01/04/2026 to 30/04/2026"),
        "opening_balance": field("10,000.00 Cr", "Opening Balance 10,000.00 Cr"),
    }, [{
        "row_text": "02/04/2026 NEFT SYNTHETIC VENDOR UTR123 5,000.00 5,000.00 Cr",
        "txn_date": "02/04/2026", "description": "NEFT SYNTHETIC VENDOR UTR123", "reference_number": "UTR123",
        "debit": "5,000.00", "balance": "5,000.00 Cr",
    }])


def statement_page2_reply() -> str:
    return reply(DocumentType.BANK_STATEMENT, {
        "bank_name": field("SYNTHETIC BANK", "SYNTHETIC BANK - page 2", page=2),
        "closing_balance": field("7,500.00 Cr", "Closing Balance 7,500.00 Cr", page=2),
    }, [{
        "row_text": "05/04/2026 SYNTHETIC SALARY CREDIT 2,500.00 7,500.00 Cr", "page": 2,
        "txn_date": "05/04/2026", "description": "SYNTHETIC SALARY CREDIT", "credit": "2,500.00",
        "balance": "7,500.00 Cr",
    }])


def invoice_pages() -> list[PageInput]:
    return [PageInput(1, INVOICE_TEXT)]


def statement_pages() -> list[PageInput]:
    return [PageInput(1, STATEMENT_PAGE_1), PageInput(2, STATEMENT_PAGE_2)]


class FakeLlm:
    """Returns queued replies in order. A reply may be an LlmError to raise."""

    provider = "fake"
    model = "fake-model"

    def __init__(self, *replies: str | LlmError, finish_reason: str = "stop") -> None:
        self.replies = list(replies)
        self.finish_reason = finish_reason
        self.requests: list[dict[str, Any]] = []
        self.reply_model: str | None = None  # model the provider reports, when not the requested one

    def complete_json(self, system: str, user: str, schema_name: str, schema: dict[str, Any]) -> LlmResponse:
        self.requests.append({"system": system, "user": user, "schema_name": schema_name, "schema": schema})
        item = self.replies.pop(0)
        if isinstance(item, LlmError):
            raise item
        return LlmResponse(content=item, model=self.reply_model or self.model, prompt_tokens=100, completion_tokens=50,
                           finish_reason=self.finish_reason)
