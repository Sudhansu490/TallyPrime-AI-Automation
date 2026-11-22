"""Matching statement transactions with TallyPrime bank-ledger entries, the bank reconciliation statement,
the read-only ledger balance request, and the reports (pure logic, SYNTHETIC data)."""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

import fitz
import openpyxl
import pytest

from tally_automation.reconciliation import matching as m
from tally_automation.reconciliation.reports import Report, Table, build_report, indian, to_pdf, to_xlsx
from tally_automation.reconciliation.transactions import apply_decisions, tally_items
from tally_automation.tally.balances import ledger_balance_request, parse_ledger_balance
from tally_automation.tally.importing import TallyEntry, TallyVoucher
from tally_automation.tally.xmlio import TallyResponseError, assert_read_only

_ids = iter(range(1, 10_000))


def bank(day: int, amount: str, *, out: bool = True, reference: str | None = None, description: str = "SYNTHETIC") -> m.BankItem:
    n = next(_ids)
    return m.BankItem(n, 7, n, date(2026, 4, day), out, Decimal(amount), description, reference)


def tally(day: int, amount: str, *, out: bool = True, narration: str = "", number: str | None = None) -> m.TallyItem:
    n = next(_ids)
    return m.TallyItem(f"g{n}#0", f"g{n}", date(2026, 4, day), "Payment" if out else "Receipt", number, out,
                       Decimal(amount), narration, None, False)


def statuses(result: m.MatchResult) -> list[str]:
    return sorted(x.status for x in result.matches)


def test_same_date_amount_and_direction_match_without_approval() -> None:
    result = m.match([bank(5, "500.00", out=False), bank(6, "200.00")], [tally(5, "500.00", out=False), tally(6, "200.00")])
    assert statuses(result) == ["matched", "matched"]
    assert not any(x.needs_approval for x in result.matches)
    assert result.bank_only == [] and result.tally_only == []


def test_direction_must_agree() -> None:
    result = m.match([bank(5, "500.00", out=True)], [tally(5, "500.00", out=False)])
    assert result.matches == [] and len(result.bank_only) == len(result.tally_only) == 1


def test_dates_apart_need_approval_and_other_candidates_are_mentioned() -> None:
    result = m.match([bank(10, "200.00")], [tally(12, "200.00"), tally(8, "200.00"), tally(30, "200.00")], window_days=3)
    (found,) = result.matches
    assert (found.status, found.kind, found.days_apart) == (m.UNCERTAIN, "date", 2) and found.needs_approval
    assert "1 other voucher(s) would also fit" in found.reason
    assert found.tally[0].voucher_date == date(2026, 4, 8)          # nearest: 8th and 12th tie, earlier first
    assert len(result.tally_only) == 2
    assert m.match([bank(10, "200.00")], [tally(12, "200.00")], window_days=0).matches == []


def test_a_shared_reference_proposes_a_match_and_flags_a_different_amount_as_partial() -> None:
    utr = "NEFT-SYNB2026041500123-SYNTHETIC CO"
    same = m.match([bank(15, "1000.00", reference="SYNB2026041500123")], [tally(25, "1000.00", narration=f"Paid {utr}")])
    assert same.matches[0].status == m.UNCERTAIN and same.matches[0].kind == "reference"
    differs = m.match([bank(15, "1000.00", description=utr)], [tally(15, "990.00", narration="UTR SYNB2026041500123")])
    (partial,) = differs.matches
    assert partial.status == m.PARTIAL and partial.difference == Decimal("-10.00")


def test_one_entry_for_several_transactions_of_a_day_is_a_partial_match() -> None:
    result = m.match([bank(9, "100.00"), bank(9, "250.00"), bank(9, "75.00")], [tally(9, "350.00")])
    (split,) = result.matches
    assert split.status == m.PARTIAL and split.kind == "split" and len(split.bank) == 2 and split.difference == 0
    assert [b.amount for b in result.bank_only] == [Decimal("75.00")]
    reverse = m.match([bank(9, "350.00")], [tally(9, "100.00"), tally(9, "250.00")])
    assert reverse.matches[0].kind == "split" and len(reverse.matches[0].tally) == 2


def test_identical_bank_charges_are_not_duplicates_but_extra_tally_entries_are() -> None:
    charges = [bank(25, "5.61") for _ in range(5)]
    result = m.match(charges, [tally(25, "5.61") for _ in range(3)])
    assert statuses(result) == ["matched"] * 3 and len(result.bank_only) == 2 and result.tally_only == []
    doubled = m.match([bank(25, "5.61")], [tally(25, "5.61"), tally(25, "5.61")])
    assert statuses(doubled) == [m.DUPLICATE, m.MATCHED] and doubled.tally_only == []


def test_signatures_are_stable_and_distinct() -> None:
    b, t = bank(5, "1.00"), tally(5, "1.00")
    first = m.Match("exact", m.MATCHED, [b], [t], "")
    assert first.signature == m.Match("exact", m.MATCHED, [b], [t], "x").signature
    assert first.signature != m.Match("exact", m.MATCHED, [b], [tally(5, "1.00")], "").signature


def test_tally_entries_on_the_bank_ledger_are_read_with_tallys_sign() -> None:
    voucher = TallyVoucher("G1", "1", date(2026, 4, 5), "Receipt", "Synthetic receipt", [], False,
                           [TallyEntry("Synthetic Bank", Decimal("-500.00")), TallyEntry("Synthetic Customer", Decimal("500.00"))],
                           number="12")
    deleted = TallyVoucher("G2", "2", date(2026, 4, 5), "Payment", None, [], True, [TallyEntry("Synthetic Bank", Decimal("9.00"))])
    (item,) = tally_items([voucher, deleted], "synthetic  bank")
    assert (item.money_out, item.amount, item.signed, item.key) == (False, Decimal("500.00"), Decimal("500.00"), "G1#0")


# ---- Bank reconciliation statement ---------------------------------------------------------------

def stored(result: m.MatchResult, bank_open: str, bank_close: str, tally_open: str, tally_close: str) -> dict:
    fig = lambda v: {"value": v, "source": "statement_opening", "statement_id": 1, "note": None}  # noqa: E731
    return {"kind": "transactions", "from": "2026-04-01", "to": "2026-04-30",
            "matches": [x.to_json() for x in result.matches], "bank_only": [b.to_json() for b in result.bank_only],
            "tally_only": [t.to_json() for t in result.tally_only], "excluded": [], "unreadable": [],
            "balances": {"bank_opening": fig(bank_open), "bank_closing": fig(bank_close),
                         "tally_opening": tally_open, "tally_closing": tally_close}}


def scenario() -> dict:
    # Bank: +500, −200 (Tally a day later), −5.61 twice (Tally has one). Tally also: −1000 not on the statement.
    result = m.match([bank(5, "500.00", out=False), bank(20, "200.00"), bank(25, "5.61"), bank(25, "5.61")],
                     [tally(5, "500.00", out=False), tally(21, "200.00"), tally(25, "5.61"), tally(28, "1000.00")])
    return stored(result, "1000.00", "1288.78", "1000.00", "294.39")


def test_reconciliation_statement_explains_every_difference() -> None:
    view = apply_decisions(scenario(), {})
    st = view["statement"]
    assert st["brought_forward"] == "0.00"
    assert (st["bank_only_total"], st["tally_only_total"]) == ("-5.61", "-1000.00")
    assert st["expected_bank_closing"] == "1288.78" and st["unexplained"] == "0.00" and st["status"] == "matched"
    assert view["counts"]["awaiting_approval"] == 1 and st["awaiting_approval"] == 1


def test_rejecting_a_match_moves_both_sides_to_unmatched_and_keeps_the_statement_balanced() -> None:
    result = scenario()
    uncertain = next(x for x in result["matches"] if x["needs_approval"])
    view = apply_decisions(result, {uncertain["signature"]: {"decision": "rejected", "note": None, "by": "x", "at": "", "run_id": 1}})
    assert view["counts"]["rejected"] == 1 and view["counts"]["bank_only"] == 2 and view["counts"]["tally_only"] == 2
    assert any(b.get("from_rejected_match") for b in view["bank_only"])
    assert view["statement"]["unexplained"] == "0.00"
    approved = apply_decisions(result, {uncertain["signature"]: {"decision": "approved", "note": None, "by": "x", "at": "", "run_id": 1}})
    assert approved["counts"]["approved"] == 1 and approved["counts"]["awaiting_approval"] == 0


def test_unknown_balances_leave_the_statement_open_instead_of_guessing() -> None:
    result = m.match([], [])
    view = apply_decisions(stored(result, None, "10.00", "10.00", "10.00"), {})
    assert view["statement"]["expected_bank_closing"] is None and view["statement"]["status"] == "unknown"


def test_a_real_difference_is_reported() -> None:
    result = scenario()
    result["balances"]["bank_closing"]["value"] = "1288.77"
    assert apply_decisions(result, {})["statement"]["unexplained"] == "-0.01"


# ---- Ledger balance request (read only) ---------------------------------------------------------------

def test_ledger_balance_request_is_read_only_and_refuses_unsafe_names() -> None:
    body = ledger_balance_request("SYNTHETIC & CO", "Synthetic Bank", date(2025, 4, 1), date(2026, 4, 30))
    assert_read_only(body)
    assert "SYNTHETIC &amp; CO" in body and "<SVTODATE TYPE=\"Date\">20260430</SVTODATE>" in body
    with pytest.raises(ValueError):
        ledger_balance_request("C", 'Bank "A"', date(2025, 4, 1), date(2026, 4, 30))


def _answer(inner: str) -> str:
    return ("<ENVELOPE><HEADER><VERSION>1</VERSION><STATUS>1</STATUS></HEADER><BODY><DATA><COLLECTION>"
            f'<LEDGER NAME="Synthetic Bank">{inner}</LEDGER></COLLECTION></DATA></BODY></ENVELOPE>')


def test_ledger_balance_parsing_uses_tallys_sign_and_its_empty_zero() -> None:
    on = date(2026, 4, 30)
    balance = parse_ledger_balance(_answer('<CLOSINGBALANCE TYPE="Amount">-294.39</CLOSINGBALANCE>'
                                           '<OPENINGBALANCE TYPE="Amount">-1000.00</OPENINGBALANCE>'), "Synthetic Bank", on)
    assert balance.bank_closing == Decimal("294.39") and balance.bank_opening == Decimal("1000.00")
    empty = parse_ledger_balance(_answer('<CLOSINGBALANCE TYPE="Amount"></CLOSINGBALANCE>'), "Synthetic Bank", on)
    assert empty.bank_closing == Decimal("0") and empty.opening is None          # missing tag: not known
    with pytest.raises(TallyResponseError):
        parse_ledger_balance(_answer(""), "Another Bank", on)


# ---- Reports ----------------------------------------------------------------------------------------

META = {"account_number": "XXXXXXXX9012", "bank_names": ["Synthetic Bank"], "label": "Transactions April",
        "kind_label": "Transactions with TallyPrime", "period_from": "2026-04-01", "period_to": "2026-04-30",
        "created_at": "16 Nov 2026", "created_by": "Synthetic admin", "generated_at": "16 Nov 2026", "stale": True,
        "ledger_name": "Synthetic Bank", "company_name": "SYNTHETIC"}


def test_reports_carry_the_same_figures_in_excel_and_pdf() -> None:
    view = apply_decisions(scenario(), {})
    view["bank_only"][0]["description"] = '=HYPERLINK("http://example.invalid","x")'
    report = build_report(META, view)
    workbook = openpyxl.load_workbook(io.BytesIO(to_xlsx(report)))
    assert workbook.sheetnames == ["Summary", "Bank reconciliation statement", "Matches", "Only in the statement",
                                   "Only in TallyPrime", "Not compared"]
    summary = [[c.value for c in row] for row in workbook["Summary"].iter_rows()]
    assert ["Account", "XXXXXXXX9012"] in summary and any(r[0] == "Warning" for r in summary)
    only = workbook["Only in the statement"]
    cells = [c for row in only.iter_rows() for c in row if isinstance(c.value, str) and "HYPERLINK" in c.value]
    assert cells and all(c.data_type == "s" for c in cells)          # text, never a formula
    brs = {row[0].value: row[1].value for row in workbook["Bank reconciliation statement"].iter_rows() if row[0].value}
    assert brs["Unexplained difference"] == 0 and brs["Balance per statement at the end"] == pytest.approx(1288.78)

    pdf = fitz.open(stream=to_pdf(report))
    text = "".join(page.get_text("text", flags=0) for page in pdf)          # flags=0: no ligatures
    assert "XXXXXXXX9012" in text and "12,88.78" not in text and "1,288.78" in text
    assert "Unexplained difference" in text and "�" not in text and "Bank reconciliation with TallyPrime - " in text


def test_indian_grouping() -> None:
    assert indian(Decimal("12345678.9")) == "1,23,45,678.90"
    assert indian(Decimal("-999.5")) == "-999.50"
    assert to_xlsx(Report("x", [], [Table("t", [("a", "text")])]))[:2] == b"PK"
