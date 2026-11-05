"""Accounting validation rules. SYNTHETIC values only."""

from __future__ import annotations

import copy
from datetime import date

from extraction_fakes import BUYER_GSTIN, SUPPLIER_GSTIN
from tally_automation.extraction.parsing import gstin_checksum, parse_value
from tally_automation.extraction.schemas import (
    INVOICE_FIELDS, LINE_ITEM_FIELDS, STATEMENT_FIELDS, TRANSACTION_FIELDS,
)
from tally_automation.validation.accounting import Check, invoice_checks, statement_checks, summarize

TODAY = date(2026, 11, 5)
# Same state (27) as the synthetic supplier: an intra-state buyer.
LOCAL_BUYER = "27BBBBB1111B1Z" + gstin_checksum("27BBBBB1111B1Z")


def build(specs, values: dict[str, str | None], **extra: dict) -> dict[str, dict]:
    """Field metadata as Phase 4 stores it, parsed with the real parser."""
    types = {s.name: s.value_type for s in specs}
    out = {}
    for name, raw in values.items():
        parsed = parse_value(raw, types[name])
        out[name] = {
            "raw": raw, "value": parsed.value, "type": types[name].value, "status": parsed.status.value,
            "method": "extracted", "flags": [], "messages": list(parsed.messages), "evidence": raw,
            "page": 1, "confidence": 0.97, "alternatives": [],
        }
    for name, meta in extra.items():
        out[name].update(meta)
    return out


def header(**values: str | None) -> dict:
    base = {
        "supplier_name": "Synthetic Supplies Pvt Ltd", "supplier_gstin": SUPPLIER_GSTIN,
        "buyer_gstin": LOCAL_BUYER, "invoice_number": "SYN-0001", "invoice_date": "01/04/2026",
        "total_taxable_value": "1,000.00", "total_cgst": "90.00", "total_sgst": "90.00",
        "round_off": "0.00", "grand_total": "1,180.00",
    }
    base.update(values)
    return build(INVOICE_FIELDS, {k: v for k, v in base.items()})


def line(**values: str | None) -> dict:
    base = {
        "description": "Synthetic Widget", "quantity": "10", "rate": "100.00", "taxable_amount": "1,000.00",
        "cgst_rate": "9%", "cgst_amount": "90.00", "sgst_rate": "9%", "sgst_amount": "90.00",
        "line_total": "1,180.00",
    }
    base.update(values)
    return build(LINE_ITEM_FIELDS, base)


def failing(checks: list[Check], severity: str | None = None) -> dict[str, Check]:
    return {
        c.code: c for c in checks
        if not c.passed and (severity is None or c.severity.value == severity) and c.severity.value != "info"
    }


# ---- Invoices ------------------------------------------------------------------------------

def test_consistent_intra_state_invoice_passes() -> None:
    checks = invoice_checks(header(), [line()], today=TODAY)
    assert summarize(checks) == (0, 0), [c.message for c in checks if not c.passed]
    passed = {c.code for c in checks if c.passed}
    assert {"line_cgst_amount", "line_sgst_amount", "total_taxable_value", "total_cgst", "grand_total",
            "supply_type"} <= passed


def test_checks_never_modify_the_extracted_fields() -> None:
    h, lines = header(grand_total="9,999.00"), [line(cgst_amount="1.00")]
    before = copy.deepcopy((h, lines))
    invoice_checks(h, lines, today=TODAY)
    assert (h, lines) == before


def test_line_tax_mismatch_is_an_error_with_calculated_value() -> None:
    checks = invoice_checks(header(total_cgst="95.00"), [line(cgst_amount="95.00", line_total="1,185.00")], today=TODAY)
    bad = failing(checks, "error")["line_cgst_amount"]
    assert (bad.row, bad.calculated, bad.printed, bad.difference) == (1, "90.00", "95.00", "5.00")


def test_header_total_must_equal_sum_of_lines() -> None:
    checks = invoice_checks(header(total_taxable_value="1,100.00"), [line()], today=TODAY)
    bad = failing(checks, "error")["total_taxable_value"]
    assert (bad.calculated, bad.printed) == ("1000.00", "1100.00")


def test_tax_amounts_missing_on_lines_are_calculated_from_rates_but_marked() -> None:
    checks = invoice_checks(header(), [line(cgst_amount=None, sgst_amount=None)], today=TODAY)
    total = next(c for c in checks if c.code == "total_cgst")
    assert total.passed and "calculated from their rates" in total.message


def test_grand_total_round_off_handling() -> None:
    # Round-off printed and consistent.
    ok = invoice_checks(header(round_off="-0.40", grand_total="1,180.00", total_cgst="90.20", total_sgst="90.20"),
                        [line(cgst_amount="90.20", sgst_amount="90.20", cgst_rate=None, sgst_rate=None, line_total=None)],
                        today=TODAY)
    assert "grand_total" not in failing(ok)
    # No round-off printed, whole-rupee total, difference below ₹1: a warning, not an error.
    rounded = invoice_checks(header(round_off=None, grand_total="1,180.00", total_cgst="90.20", total_sgst="90.20"),
                             [line(cgst_amount="90.20", sgst_amount="90.20", cgst_rate=None, sgst_rate=None, line_total=None)],
                             today=TODAY)
    assert failing(rounded)["grand_total"].severity.value == "warning"
    # A real difference is an error.
    wrong = invoice_checks(header(grand_total="1,185.00"), [line()], today=TODAY)
    assert failing(wrong, "error")["grand_total"].difference == "5.00"


def test_igst_on_intra_state_supply_is_a_warning_and_mixing_is_an_error() -> None:
    igst_line = line(cgst_rate=None, cgst_amount=None, sgst_rate=None, sgst_amount=None,
                     igst_rate="18%", igst_amount="180.00")
    checks = invoice_checks(header(total_cgst=None, total_sgst=None, total_igst="180.00"), [igst_line], today=TODAY)
    assert failing(checks, "warning")["supply_type"].message.startswith("Same states")
    # Different states with IGST is correct.
    inter = invoice_checks(header(buyer_gstin=BUYER_GSTIN, total_cgst=None, total_sgst=None, total_igst="180.00"),
                           [igst_line], today=TODAY)
    assert "supply_type" not in failing(inter)
    mixed = invoice_checks(header(total_igst="180.00", grand_total="1,360.00"),
                           [line(igst_rate="18%", igst_amount="180.00", line_total=None)], today=TODAY)
    assert {"line_igst_with_cgst", "supply_type"} <= set(failing(mixed, "error"))


def test_cgst_and_sgst_rates_must_match() -> None:
    checks = invoice_checks(header(total_sgst="60.00", grand_total="1,150.00"),
                            [line(sgst_rate="6%", sgst_amount="60.00", line_total=None)], today=TODAY)
    assert "line_cgst_sgst_rate" in failing(checks, "error")


def test_line_total_printed_before_tax_is_accepted() -> None:
    checks = invoice_checks(header(), [line(line_total="1,000.00")], today=TODAY)
    note = next(c for c in checks if c.code == "line_total")
    assert note.passed and "before tax" in note.message


def test_extraction_problems_are_reported() -> None:
    h = header(invoice_number=None, supplier_gstin="27AAAAA0000A1ZX")
    h["invoice_number"]["flags"] = ["required_missing"]
    h["grand_total"]["flags"] = ["conflict"]
    h["supplier_name"]["flags"] = ["low_confidence"]
    checks = invoice_checks(h, [line()], today=TODAY)
    errors = [c for c in checks if c.counts_as_error and c.category == "extraction"]
    assert {(c.code, c.fields[0]) for c in errors} == {
        ("required_missing", "invoice_number"), ("value_invalid", "supplier_gstin"), ("value_conflict", "grand_total"),
    }
    assert failing(checks, "warning")["low_confidence"].fields == ["supplier_name"]


def test_dates_and_gstins() -> None:
    checks = invoice_checks(header(invoice_date="01/12/2026", buyer_gstin=SUPPLIER_GSTIN), [line()], today=TODAY)
    assert {"invoice_date_future", "same_gstin"} <= set(failing(checks, "error"))


def test_invoice_without_lines_cannot_be_fully_checked() -> None:
    checks = invoice_checks(header(), [], today=TODAY)
    assert "no_line_items" in failing(checks, "warning")
    assert next(c for c in checks if c.code == "grand_total").passed  # header totals still add up


# ---- Bank statements ------------------------------------------------------------------------

def statement(**values: str | None) -> dict:
    base = {"account_number": "123456789012", "period_from": "01/04/2026", "period_to": "30/04/2026",
            "opening_balance": "10,000.00 Cr", "closing_balance": "7,500.00 Cr"}
    base.update(values)
    return build(STATEMENT_FIELDS, base)


def txn(date_: str, debit: str | None, credit: str | None, balance: str | None, description: str = "SYNTHETIC") -> dict:
    return build(TRANSACTION_FIELDS, {"txn_date": date_, "description": description, "debit": debit,
                                      "credit": credit, "balance": balance})


ROWS = [txn("02/04/2026", "5,000.00", None, "5,000.00 Cr"), txn("05/04/2026", None, "2,500.00", "7,500.00 Cr")]


def test_consistent_statement_passes() -> None:
    checks = statement_checks(statement(), ROWS, today=TODAY)
    assert summarize(checks) == (0, 0), [c.message for c in checks if not c.passed]
    assert {"running_balance", "closing_balance"} <= {c.code for c in checks if c.passed}


def test_swapped_debit_and_credit_is_detected() -> None:
    rows = [txn("02/04/2026", None, "5,000.00", "5,000.00 Cr"), ROWS[1]]
    bad = failing(statement_checks(statement(), rows, today=TODAY), "error")
    assert "swapped" in bad["running_balance"].message
    assert (bad["running_balance"].row, bad["running_balance"].calculated) == (1, "15000.00")
    assert "closing_balance" in bad


def test_one_balance_error_does_not_cascade() -> None:
    rows = [txn("02/04/2026", "5,000.00", None, "5,100.00 Cr"), txn("05/04/2026", None, "2,500.00", "7,600.00 Cr")]
    checks = statement_checks(statement(closing_balance="7,600.00 Cr"), rows, today=TODAY)
    assert [c.row for c in checks if c.code == "running_balance" and not c.passed] == [1]
    summary = next(c for c in checks if c.code == "running_balance_summary")
    assert summary.message == "Running balance is consistent on 1 of 2 checked transaction(s)."


def test_transaction_amount_rules() -> None:
    rows = [txn("02/04/2026", "5,000.00", "5,000.00", "10,000.00 Cr"), txn("05/04/2026", None, None, "10,000.00 Cr")]
    bad = failing(statement_checks(statement(closing_balance=None), rows, today=TODAY), "error")
    assert {"debit_and_credit", "no_amount"} <= set(bad)


def test_newest_first_statements_are_checked_bottom_up() -> None:
    checks = statement_checks(statement(), list(reversed(ROWS)), today=TODAY)
    assert summarize(checks)[0] == 0
    assert any(c.code == "row_order" and c.passed for c in checks)


def test_overdraft_balances_with_dr_markers() -> None:
    rows = [txn("02/04/2026", "1,500.00", None, "500.00 Dr")]
    checks = statement_checks(statement(opening_balance="1,000.00 Cr", closing_balance="500.00 Dr"), rows, today=TODAY)
    assert summarize(checks)[0] == 0


def test_dates_outside_period_and_repeated_rows() -> None:
    rows = [txn("02/04/2026", "100.00", None, "9,900.00 Cr"), txn("02/04/2026", "100.00", None, "9,800.00 Cr"),
            txn("02/05/2026", "100.00", None, "9,700.00 Cr")]
    checks = statement_checks(statement(closing_balance="9,700.00 Cr"), rows, today=TODAY)
    warnings = failing(checks, "warning")
    assert warnings["txn_outside_period"].row == 3
    assert warnings["repeated_transaction"].row == 2
    assert "probably real" in warnings["repeated_transaction"].message
    assert summarize(checks)[0] == 0
