"""Debit/credit amounts the LLM skipped, read from a ruled statement table. SYNTHETIC data only."""

from __future__ import annotations

from extraction_fakes import FakeLlm, field, reply
from tally_automation.db.models import DocumentType
from tally_automation.extraction.service import PageInput, extract_document
from tally_automation.extraction.table_amounts import correct_amount_columns, fill_skipped_amounts

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


def test_an_amount_in_the_wrong_column_is_moved_to_the_printed_column() -> None:
    result, pages = extract([row(ROW_DEBIT, credit="1,000.00", balance="10,000.00 Cr."),
                             row(ROW_CREDIT, debit="2,500.00", balance="12,500.00 Cr.")])
    assert correct_amount_columns(result, pages, low_confidence=0.8) == 2
    debit_row, credit_row = result.rows
    assert (debit_row.fields["debit"].value, debit_row.fields["credit"].raw) == ("1000.00", None)
    assert (credit_row.fields["credit"].value, credit_row.fields["debit"].raw) == ("2500.00", None)
    assert debit_row.fields["debit"].method == "table" and "column_corrected" in debit_row.fields["debit"].flags


def test_a_stray_second_amount_is_cleared_and_right_rows_are_left_alone() -> None:
    # The printed debit in the credit column, plus another number (a cheque number) in the debit column.
    result, pages = extract([row(ROW_DEBIT, debit="123,456.00", credit="1,000.00"),
                             row(ROW_CREDIT, credit="2,500.00")])
    assert correct_amount_columns(result, pages, low_confidence=0.8) == 1
    fixed, untouched = result.rows
    assert (fixed.fields["debit"].value, fixed.fields["credit"].raw) == ("1000.00", None)
    assert untouched.fields["credit"].method == "extracted"
    # Right column plus a stray amount on the other side: the stray one is cleared.
    result, pages = extract([row(ROW_DEBIT, debit="1,000.00", credit="77.00")])
    assert correct_amount_columns(result, pages, low_confidence=0.8) == 1
    assert (result.rows[0].fields["debit"].value, result.rows[0].fields["credit"].raw) == ("1000.00", None)
    # A different amount than the table prints is never "corrected": the reviewer decides.
    result, pages = extract([row(ROW_DEBIT, credit="9,999.00")])
    assert correct_amount_columns(result, pages, low_confidence=0.8) == 0


# A plain-text statement (no drawn table): columns kept as spaces, wrapped rows print the amount below.
LAYOUT_HEADER = "TRAN DATE     NARRATION                     CHQ.NO.     WITHDRAWALS(DR)     DEPOSITS(CR)     BALANCE(INR)"
LAYOUT_ROW_1 = "30/10/2026    NEFT-SYNB00000001-SYNTHETIC                                                       1,10,000.00Dr"
LAYOUT_AMT_1 = "                                                               10,000.00"
LAYOUT_ROW_2 = "30/10/2026    NEFT-SYNB00000002-SYNTHETIC                                                       1,00,000.00Dr"
LAYOUT_AMT_2 = "                                                                                 2,500.00"
LAYOUT_TEXT = "\n".join(["SYNTHETIC BANK", LAYOUT_HEADER, LAYOUT_ROW_1, LAYOUT_AMT_1, "  SYNTHETIC PARTY",
                         LAYOUT_ROW_2, LAYOUT_AMT_2])


def test_plain_text_statement_amount_printed_below_the_row_is_read_from_its_column() -> None:
    result, pages = extract([row(LAYOUT_ROW_1, balance="1,10,000.00Dr"), row(LAYOUT_ROW_2, balance="1,00,000.00Dr")],
                            text=LAYOUT_TEXT)
    assert fill_skipped_amounts(result, pages, low_confidence=0.8) == 2
    first, second = result.rows
    assert (first.fields["debit"].value, first.fields["credit"].raw) == ("10000.00", None)
    assert (second.fields["credit"].value, second.fields["debit"].raw) == ("2500.00", None)
    assert first.fields["debit"].method == "table" and "line below the row" in first.fields["debit"].messages[-1]


def test_plain_text_statement_is_left_alone_when_unclear() -> None:
    # Two amounts below the row: which one belongs to it is not clear.
    two = "\n".join(["SYNTHETIC BANK", LAYOUT_HEADER, LAYOUT_ROW_1, LAYOUT_AMT_1 + "      5.00"])
    result, pages = extract([row(LAYOUT_ROW_1)], text=two)
    assert fill_skipped_amounts(result, pages, low_confidence=0.8) == 0
    # The next transaction starts before any lone amount: nothing to take.
    nxt = "\n".join(["SYNTHETIC BANK", LAYOUT_HEADER, LAYOUT_ROW_1, LAYOUT_ROW_2, LAYOUT_AMT_2])
    result, pages = extract([row(LAYOUT_ROW_1)], text=nxt)
    assert fill_skipped_amounts(result, pages, low_confidence=0.8) == 0


def test_plain_text_statement_amount_in_the_wrong_column_is_moved() -> None:
    from tally_automation.extraction.table_amounts import correct_amount_columns
    # The model quoted the wrapped lines too, and put the debit printed below the row in the credit column.
    quoted = "\n".join([LAYOUT_ROW_1, LAYOUT_AMT_1.strip(), "SYNTHETIC PARTY"])
    result, pages = extract([row(quoted, credit="10,000.00", balance="1,10,000.00Dr")], text=LAYOUT_TEXT)
    assert correct_amount_columns(result, pages, low_confidence=0.8) == 1
    fixed = result.rows[0]
    assert (fixed.fields["debit"].value, fixed.fields["credit"].raw) == ("10000.00", None)


# ---- Statements laid out in text columns (SYNTHETIC) ------------------------------------------------

def _columns_line(date_: str, narration: str, debit: str = "", credit: str = "", balance: str = "") -> str:
    """A dated line with the amounts right-aligned under their headings (debit to 60, credit to 80, balance to 100)."""
    left = f"{date_}  {date_}  {narration}"
    out = left.ljust(60 - len(debit)) + debit
    out = out.ljust(80 - len(credit)) + credit
    return out.ljust(100 - len(balance)) + balance


COLUMNS_HEADER = "DATE        VALUE DATE  NARRATION".ljust(45) + "WITHDRAWAL (DR)".rjust(15) + "DEPOSIT (CR)".rjust(20) + "BALANCE".rjust(20)
COLUMNS_ROWS = [
    _columns_line("01/04/2026", "NEFT-SYNB000001-SYNTHETIC VENDOR", debit="1,000.00", balance="9,000.00Cr"),
    _columns_line("01/04/2026", "Charges for SYNTHETIC payment", debit="5.00", balance="8,995.00Cr"),
    _columns_line("02/04/2026", "NEFT-SYNB000002-SYNTHETIC CUSTOMER", credit="2,500.00", balance="11,495.00Cr"),
]
COLUMNS_TEXT = "\n".join(["SYNTHETIC BANK - STATEMENT OF ACCOUNT", COLUMNS_HEADER, *COLUMNS_ROWS])


def test_rows_proven_by_the_running_balance_replace_the_models_and_add_skipped_ones() -> None:
    from tally_automation.extraction.table_amounts import rows_from_layout
    # The model skipped the charge and put the deposit in the debit column.
    result, pages = extract([row(COLUMNS_ROWS[0], txn_date="01/04/2026", debit="1,000.00", balance="9,000.00Cr", description="SYNTHETIC VENDOR"),
                             row(COLUMNS_ROWS[2], debit="2,500.00", balance="11,495.00Cr")], text=COLUMNS_TEXT)
    assert rows_from_layout(result, pages, low_confidence=0.8) == 3
    first, charge, deposit = result.rows
    assert (charge.fields["debit"].value, charge.fields["credit"].raw) == ("5.00", None)
    assert (deposit.fields["credit"].value, deposit.fields["debit"].raw, deposit.fields["balance"].value) == (
        "2500.00", None, "11495.00")
    assert first.fields["description"].value == "SYNTHETIC VENDOR"           # the model's narration is kept
    assert "running balance confirms all 3" in result.document_check_issues[-1]


def test_rows_are_not_replaced_when_the_running_balance_does_not_prove_them() -> None:
    from tally_automation.extraction.table_amounts import rows_from_layout
    broken = COLUMNS_TEXT.replace("11,495.00Cr", "99,999.00Cr")
    result, pages = extract([row(COLUMNS_ROWS[0], debit="1,000.00")], text=broken)
    assert rows_from_layout(result, pages, low_confidence=0.8) == 0 and len(result.rows) == 1
