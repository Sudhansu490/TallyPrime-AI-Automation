"""Monthly and yearly balance reconciliation, and missing statements (pure logic, SYNTHETIC figures)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from tally_automation.reconciliation import balances as b
from tally_automation.reconciliation.coverage import coverage
from tally_automation.reconciliation.periods import (
    fy_bounds, fy_label, fy_start_year, months_between, parse_fy, parse_month,
)


def f(value: str | None, status: str = "valid") -> dict:
    return {"raw": value, "value": value, "status": status if value is not None else "missing"}


def stmt(sid: int, frm: str | None, to: str | None, opening: str | None, closing: str | None,
         rows: list[tuple[str | None, str | None, str | None, str | None]] = (), validation: str | None = "passed",
         ) -> b.StatementData:
    header = {"account_number": f("123456789012"), "bank_name": f("Synthetic Bank"),
              "period_from": f(frm), "period_to": f(to), "opening_balance": f(opening), "closing_balance": f(closing)}
    row_fields = [{"txn_date": f(d), "debit": f(dr), "credit": f(cr), "balance": f(bal)} for d, dr, cr, bal in rows]
    return b.prepare_statement(sid, sid * 10, f"synthetic-{sid}.pdf", header, row_fields, validation)


def months(*statements: b.StatementData, first: str, last: str) -> dict[str, b.PeriodResult]:
    timeline = b.build_timeline(list(statements))
    return {r.label: r for r in b.monthly(timeline, parse_month(first), parse_month(last))}


APRIL = stmt(1, "2026-04-01", "2026-04-30", "1000.00", "1300.00",
             [("2026-04-05", None, "500.00", "1500.00"), ("2026-04-20", "200.00", None, "1300.00")])
MAY = stmt(2, "2026-05-01", "2026-05-31", "1300.00", "1250.00", [("2026-05-10", "50.00", None, "1250.00")])


# ---- Periods ---------------------------------------------------------------------------------------

def test_indian_financial_year_runs_april_to_march() -> None:
    assert fy_start_year(date(2026, 3, 31)) == 2025
    assert fy_start_year(date(2026, 4, 1)) == 2026
    assert fy_label(2025) == "2025-26" and fy_label(1999) == "1999-00"
    assert parse_fy("2025-26") == 2025
    assert fy_bounds(2025) == (date(2025, 4, 1), date(2026, 3, 31))
    for bad in ("2025-27", "2025", "25-26", "2025/26"):
        with pytest.raises(ValueError):
            parse_fy(bad)
    with pytest.raises(ValueError):
        parse_month("2026-13")
    assert [m for m in months_between((2025, 11), (2026, 2))] == [(2025, 11), (2025, 12), (2026, 1), (2026, 2)]


# ---- Monthly -------------------------------------------------------------------------------------

def test_consecutive_months_that_agree_are_matched() -> None:
    result = months(APRIL, MAY, first="2026-04", last="2026-05")
    april, may = result["2026-04"], result["2026-05"]
    assert april.status == may.status == b.MATCHED
    assert (april.opening.value, april.credits, april.debits) == (Decimal("1000.00"), Decimal("500.00"), Decimal("200.00"))
    assert april.calculated_closing == april.closing.value == Decimal("1300.00") and april.difference == 0
    assert april.opening.source == "statement_opening" and april.closing.source == "statement_closing"
    assert may.previous.status == b.MATCHED and may.previous.difference == 0 and not may.previous.same_statement
    # March has no statement: April's opening cannot be compared, but it is not a mismatch either.
    assert april.previous.status == b.UNKNOWN


def test_opening_that_differs_from_the_previous_closing_is_a_mismatch() -> None:
    may = stmt(2, "2026-05-01", "2026-05-31", "1350.00", "1300.00", [("2026-05-10", "50.00", None, "1300.00")])
    result = months(APRIL, may, first="2026-04", last="2026-05")
    link = result["2026-05"].previous
    assert link.status == b.MISMATCH and link.difference == Decimal("50.00")
    assert result["2026-05"].status == b.MATCHED          # the month itself adds up
    assert b.summarize(list(result.values()))["opening_mismatches"] == 1


@pytest.mark.parametrize("printed, difference", [("1301.00", "1.00"), ("1299.99", "-0.01")])
def test_closing_that_does_not_add_up_is_a_mismatch_to_the_paisa(printed: str, difference: str) -> None:
    april = stmt(1, "2026-04-01", "2026-04-30", "1000.00", printed,
                 [("2026-04-05", None, "500.00", "1500.00"), ("2026-04-20", "200.00", None, "1300.00")])
    result = months(april, first="2026-04", last="2026-04")["2026-04"]
    assert result.status == b.MISMATCH
    assert result.calculated_closing == Decimal("1300.00") and result.difference == Decimal(difference)


def test_decimal_arithmetic_is_exact() -> None:
    april = stmt(1, "2026-04-01", "2026-04-30", "0.10", "0.30", [("2026-04-02", None, "0.20", "0.30")])
    assert months(april, first="2026-04", last="2026-04")["2026-04"].difference == Decimal("0")


def test_a_missing_month_is_missing_never_zero() -> None:
    june = stmt(3, "2026-06-01", "2026-06-30", "1300.00", "1300.00")
    result = months(APRIL, june, first="2026-04", last="2026-06")
    may = result["2026-05"]
    assert may.status == b.MISSING
    assert may.opening.value is None and may.closing.value is None and may.credits is None and may.debits is None
    assert may.to_json()["opening"]["value"] is None and may.to_json()["total_credits"] is None
    assert result["2026-06"].previous.status == b.UNKNOWN          # May's closing is not known
    cover = coverage(b.build_timeline([APRIL, june]))
    assert cover.missing_months == ["2026-05"] and cover.missing == [(date(2026, 5, 1), date(2026, 5, 31))]


def test_a_statement_over_several_months_is_split_by_its_printed_running_balance() -> None:
    both = stmt(1, "2026-04-01", "2026-05-31", "1000.00", "1250.00",
                [("2026-04-05", None, "500.00", "1500.00"), ("2026-04-20", "200.00", None, "1300.00"),
                 ("2026-05-10", "50.00", None, "1250.00")])
    result = months(both, first="2026-04", last="2026-05")
    april, may = result["2026-04"], result["2026-05"]
    assert april.closing.value == Decimal("1300.00") and april.closing.source == "row_balance"
    assert may.opening.value == Decimal("1300.00") and may.previous.same_statement
    assert april.status == may.status == b.MATCHED
    assert (may.credits, may.debits, may.closing.source) == (Decimal("0"), Decimal("50.00"), "statement_closing")


def test_a_month_without_transactions_inside_a_statement_carries_the_balance() -> None:
    quarter = stmt(1, "2026-04-01", "2026-06-30", "1000.00", "900.00",
                   [("2026-04-05", "50.00", None, "950.00"), ("2026-06-03", "50.00", None, "900.00")])
    may = months(quarter, first="2026-05", last="2026-05")["2026-05"]
    assert may.status == b.MATCHED
    assert may.opening.value == may.closing.value == Decimal("950.00")
    assert (may.credits, may.debits) == (Decimal("0"), Decimal("0"))


def test_a_month_covered_only_in_part_is_partial() -> None:
    late = stmt(1, "2026-04-10", "2026-04-30", "1000.00", "1000.00")
    april = months(late, first="2026-04", last="2026-04")["2026-04"]
    assert april.status == b.PARTIAL and april.opening.value is None
    assert april.missing_ranges == [(date(2026, 4, 1), date(2026, 4, 9))]
    assert april.closing.value == Decimal("1000.00")
    assert coverage(b.build_timeline([late]), date(2026, 4, 1), date(2026, 4, 30)).partial_months[0]["missing_days"] == 9


def test_overlapping_statements_that_agree_are_not_counted_twice() -> None:
    again = stmt(5, "2026-04-01", "2026-04-30", "1000.00", "1300.00",
                 [("2026-04-05", None, "500.00", "1500.00"), ("2026-04-20", "200.00", None, "1300.00")])
    april = months(APRIL, again, first="2026-04", last="2026-04")["2026-04"]
    assert april.status == b.MATCHED and april.credits == Decimal("500.00")
    assert april.statement_ids == [1, 5] and any("agree" in n for n in april.notes)


def test_overlapping_statements_that_disagree_are_a_conflict() -> None:
    other = stmt(5, "2026-04-15", "2026-05-15", "1500.00", "1400.00",
                 [("2026-04-20", "100.00", None, "1400.00")])
    result = months(APRIL, other, first="2026-04", last="2026-05")
    assert result["2026-04"].status == b.CONFLICT
    assert result["2026-04"].conflicts[0].to_json()["statement_ids"] == [1, 5]


def test_overdrawn_dr_balances_are_negative() -> None:
    od = stmt(1, "2026-04-01", "2026-04-30", "-500.00", "-200.00", [("2026-04-03", None, "300.00", "-200.00")])
    april = months(od, first="2026-04", last="2026-04")["2026-04"]
    assert april.status == b.MATCHED and april.calculated_closing == Decimal("-200.00")


def test_a_transaction_without_an_amount_leaves_totals_unknown() -> None:
    gap = stmt(1, "2026-04-01", "2026-04-30", "1000.00", "900.00", [("2026-04-03", None, None, "900.00")])
    april = months(gap, first="2026-04", last="2026-04")["2026-04"]
    assert april.status == b.INCOMPLETE and april.credits is None and april.calculated_closing is None


def test_unread_amounts_are_not_used() -> None:
    header_bad = stmt(1, "2026-04-01", "2026-04-30", None, "1000.00")
    assert header_bad.opening.value is None
    april = months(header_bad, first="2026-04", last="2026-04")["2026-04"]
    assert april.status == b.INCOMPLETE


def test_opening_not_printed_is_worked_out_from_the_first_transaction_and_labelled() -> None:
    no_opening = stmt(1, "2026-04-01", "2026-04-30", None, "1300.00",
                      [("2026-04-05", None, "500.00", "1500.00"), ("2026-04-20", "200.00", None, "1300.00")])
    assert no_opening.opening.value == Decimal("1000.00") and no_opening.opening.source == "derived_first_row"


def test_period_not_printed_is_taken_from_transaction_dates_and_flagged() -> None:
    undated = stmt(1, None, None, "1000.00", "1300.00",
                   [("2026-04-05", None, "500.00", "1500.00"), ("2026-04-20", "200.00", None, "1300.00")])
    assert undated.period_inferred and (undated.period_from, undated.period_to) == (date(2026, 4, 5), date(2026, 4, 20))
    april = months(undated, first="2026-04", last="2026-04")["2026-04"]
    assert april.status == b.PARTIAL and april.inferred           # 1–4 and 21–30 April are not proven covered
    nothing = stmt(2, None, None, "1000.00", "1000.00")
    assert not nothing.usable and b.build_timeline([nothing]).unusable == [nothing]


def test_newest_first_statements_are_read_bottom_up() -> None:
    newest_first = stmt(1, "2026-04-01", "2026-05-31", "1000.00", "1250.00",
                        [("2026-05-10", "50.00", None, "1250.00"), ("2026-04-20", "200.00", None, "1300.00"),
                         ("2026-04-05", None, "500.00", "1500.00")])
    assert newest_first.order == "descending"
    result = months(newest_first, first="2026-04", last="2026-05")
    assert result["2026-04"].closing.value == Decimal("1300.00") and result["2026-05"].status == b.MATCHED


def test_failed_validation_marks_figures_unverified() -> None:
    failed = stmt(1, "2026-04-01", "2026-04-30", "1000.00", "1000.00", validation="failed")
    assert failed.unverified
    assert months(failed, first="2026-04", last="2026-04")["2026-04"].unverified


# ---- Financial years ---------------------------------------------------------------------------------

def year_statement(sid: int, start_year: int, opening: str, closing: str, rows) -> b.StatementData:
    return stmt(sid, f"{start_year}-04-01", f"{start_year + 1}-03-31", opening, closing, rows)


def test_financial_year_closing_is_compared_with_the_next_years_opening() -> None:
    fy25 = year_statement(1, 2025, "1000.00", "1600.00", [("2025-06-01", None, "1000.00", "2000.00"),
                                                          ("2026-02-01", "400.00", None, "1600.00")])
    fy26 = year_statement(2, 2026, "1650.00", "1650.00", [])
    years = b.yearly(b.build_timeline([fy25, fy26]), 2025, 2026)
    assert [y.label for y in years] == ["2025-26", "2026-27"]
    assert years[0].status == b.MATCHED
    assert (years[0].credits, years[0].debits, years[0].closing.value) == (Decimal("1000.00"), Decimal("400.00"), Decimal("1600.00"))
    assert years[1].previous.status == b.MISMATCH and years[1].previous.difference == Decimal("50.00")
    assert years[0].previous.status == b.UNKNOWN                  # nothing before April 2025


def test_a_year_with_missing_months_is_partial_and_lists_them() -> None:
    april = stmt(1, "2025-04-01", "2025-04-30", "1000.00", "1000.00")
    march = stmt(2, "2026-03-01", "2026-03-31", "1000.00", "1000.00")
    year = b.yearly(b.build_timeline([april, march]), 2025, 2025)[0]
    assert year.status == b.PARTIAL and year.credits is None and year.calculated_closing is None
    assert year.missing_months == [f"2025-{m:02d}" for m in range(5, 13)] + ["2026-01", "2026-02"]
    assert year.opening.value == Decimal("1000.00") and year.closing.value == Decimal("1000.00")


def test_a_year_without_statements_is_missing_and_selected_ranges_are_checked() -> None:
    timeline = b.build_timeline([APRIL])
    years = b.yearly(timeline, 2024, 2026)
    assert [y.status for y in years] == [b.MISSING, b.MISSING, b.PARTIAL]
    assert coverage(timeline, date(2024, 4, 1), date(2026, 4, 30)).missing_years == ["2024-25", "2025-26"]
    with pytest.raises(ValueError):
        b.yearly(timeline, 2026, 2025)
    with pytest.raises(ValueError):
        b.monthly(timeline, (2026, 5), (2026, 4))


def test_a_balance_jump_between_statements_inside_a_month_is_a_mismatch() -> None:
    first_half = stmt(1, "2026-04-01", "2026-04-15", "1000.00", "1000.00")
    second_half = stmt(2, "2026-04-16", "2026-04-30", "994.39", "994.39")
    april = months(first_half, second_half, first="2026-04", last="2026-04")["2026-04"]
    assert april.status == b.MISMATCH and april.difference == Decimal("-5.61")
    assert april.breaks == [{"on": "2026-04-16", "status": b.MISMATCH, "difference": "-5.61",
                             "previous_document_id": 10, "next_document_id": 20,
                             "previous_closing": "1000.00", "next_opening": "994.39"}]
    assert april.document_ids == [10, 20]
