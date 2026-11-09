"""Debit/credit amounts the LLM skipped, read from a ruled statement table. SYNTHETIC data only."""

from __future__ import annotations

from extraction_fakes import FakeLlm, field, reply
from tally_automation.db.models import DocumentType
from tally_automation.extraction.service import PageInput, extract_document
from tally_automation.extraction.table_amounts import fill_skipped_amounts

HEADER = "Txn No. | Txn Date | Description | Dr Amount | Cr Amount | Balance"
ROW_CREDIT = "S0000001 | 02-04-2026 | NEFT_IN:SYNB00000001/SYNTHETIC CUSTOMER |  | 2,500.00 | 12,500.00 Cr."
ROW_DEBIT = "S0000002 | 02-04-2026 | NEFT_OUT:SYNB00000002/SYNTHETIC VENDOR | 1,000.00 |  | 10,000.00 Cr."
TEXT = "\n".join(["SYNTHETIC BANK ACCOUNT STATEMENT", HEADER, ROW_CREDIT, ROW_DEBIT])


def extract(rows: list[dict], text: str = TEXT):
    pages = [PageInput(1, text)]
    llm = FakeLlm(reply(DocumentType.BANK_STATEMENT, {"account_number": field("123456789012")}, rows))
    result = extract_document(pages, DocumentType.BANK_STATEMENT, llm, pages_per_call=3, low_confidence=0.8)
    return result, pages


def row(row_text: str, **values) -> dict:
    return {"row_text": row_text, "txn_date": "02-04-2026", "description": "SYNTHETIC", **values}


def test_skipped_credit_is_copied_from_its_column_and_marked_for_review() -> None:
    result, pages = extract([row(ROW_CREDIT, balance="12,500.00 Cr."), row(ROW_DEBIT, debit="1,000.00", balance="10,000.00 Cr.")])
    assert fill_skipped_amounts(result, pages, low_confidence=0.8) == 1
    credit = result.rows[0].fields["credit"]
    assert (credit.raw, credit.value, credit.method, credit.status.value) == ("2,500.00", "2500.00", "table", "warning")
    assert "read_from_table" in credit.flags and credit.evidence == ROW_CREDIT
    assert result.rows[0].fields["debit"].raw is None
    # A row the model read completely is left alone.
    assert result.rows[1].fields["debit"].method == "extracted"


def test_skipped_debit_is_copied_from_its_column() -> None:
    result, pages = extract([row(ROW_DEBIT, balance="10,000.00 Cr.")])
    assert fill_skipped_amounts(result, pages, low_confidence=0.8) == 1
    assert result.rows[0].fields["debit"].value == "1000.00" and result.rows[0].fields["credit"].raw is None


def test_nothing_is_filled_without_a_clear_table() -> None:
    # Not a table row (plain layout text): nothing to copy from.
    plain = "02-04-2026 NEFT_IN SYNTHETIC CUSTOMER 2,500.00 12,500.00 Cr."
    result, pages = extract([row(plain)], text="SYNTHETIC BANK\n" + plain)
    assert fill_skipped_amounts(result, pages, low_confidence=0.8) == 0
    # Header without debit/credit column names.
    vague = "\n".join(["No | Date | Narration | Amount 1 | Amount 2 | Balance", ROW_CREDIT])
    result, pages = extract([row(ROW_CREDIT)], text=vague)
    assert fill_skipped_amounts(result, pages, low_confidence=0.8) == 0
    # A row text the model made up (not a line of the page) is never used.
    result, pages = extract([row(ROW_CREDIT.replace("2,500.00", "9,999.00"))])
    assert fill_skipped_amounts(result, pages, low_confidence=0.8) == 0
    # Both cells filled: ambiguous, left for the reviewer.
    both = "S0000003 | 02-04-2026 | SYNTHETIC | 5.00 | 5.00 | 10,000.00 Cr."
    result, pages = extract([row(both)], text="\n".join([HEADER, both]))
    assert fill_skipped_amounts(result, pages, low_confidence=0.8) == 0


def test_invoices_are_never_touched() -> None:
    result, pages = extract([row(ROW_CREDIT)])
    result.document_type = DocumentType.PURCHASE_INVOICE
    assert fill_skipped_amounts(result, pages, low_confidence=0.8) == 0


def test_worker_fills_a_skipped_amount_end_to_end(client) -> None:
    from synthetic_files import RULED_STATEMENT_ROWS, make_ruled_statement_pdf
    from test_extraction_api import process, upload

    rows = [" | ".join(r).rstrip(" |") for r in RULED_STATEMENT_ROWS[1:]]
    document_id = upload(client, make_ruled_statement_pdf(), "bank_statement")
    process(client, FakeLlm(reply(DocumentType.BANK_STATEMENT, {"opening_balance": field(None)}, [
        row(rows[0], debit="1,000.00", balance="9,000.00 Cr."),
        row(rows[1], debit="5.90", balance="8,994.10 Cr."),
        row(rows[2], balance="11,494.10 Cr."),  # the model skipped this credit
    ])))
    data = client.get(f"/api/v1/documents/{document_id}/extraction").json()["data"]
    credit = {f["name"]: f for f in data["rows"][2]["fields"]}["credit"]
    assert (credit["raw"], credit["method"], credit["status"]) == ("2,500.00", "table", "warning")
    # With the copied credit the balances add up (the synthetic page has no account number,
    # so only the amount and balance checks are looked at here).
    checks = client.get(f"/api/v1/documents/{document_id}/validation").json()["checks"]
    failed = {c["code"] for c in checks if not c["passed"]}
    assert not failed & {"no_amount", "running_balance", "closing_balance", "closing_last_row"}, failed
    assert any(c["code"] == "running_balance" and c["passed"] for c in checks)
