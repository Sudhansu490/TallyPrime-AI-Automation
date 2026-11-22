"""Monthly and yearly balance reconciliation of one bank account, from its statements.

Pure functions: no database, no TallyPrime. Every figure comes from what the statements print, and says
where it came from:

* ``statement_opening`` / ``statement_closing`` – the balance printed in the statement's header;
* ``row_balance`` – the running balance printed after a transaction (used at month ends that fall
  inside a statement);
* ``last_row_balance`` – the statement printed no closing balance; the balance after its last
  transaction is the closing balance;
* ``derived_first_row`` – the statement printed no opening balance; it is the first transaction's
  printed balance with that transaction taken back out (first balance − credit + debit).

A figure that the statements do not print or prove is ``None`` with a reason. A month or year that no
statement covers is *missing*: it never counts as a zero balance.

Formula: calculated closing = opening + total credits − total debits; difference = actual − calculated.
All amounts are ``Decimal``; a difference of 0.01 is a difference.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from tally_automation.reconciliation.periods import (
    Month, day_after, day_before, fy_bounds, fy_label, fy_months, month_bounds, month_label, month_of,
    months_between,
)
from tally_automation.validation.accounting import amount, chronological_rows, day, money, text

ZERO = Decimal("0")

# Statuses, most serious first (used to summarise).
MATCHED = "matched"
MISMATCH = "mismatch"
MISSING = "missing"
PARTIAL = "partial"
INCOMPLETE = "incomplete"
CONFLICT = "conflict"
UNKNOWN = "unknown"


# ---- Statements -----------------------------------------------------------------------------------

@dataclass(frozen=True)
class Row:
    number: int                    # position as printed (1 = first row on the statement)
    txn_date: date | None
    debit: Decimal | None
    credit: Decimal | None
    balance: Decimal | None
    transaction_id: int | None = None
    description: str | None = None
    reference: str | None = None

    @property
    def amount_known(self) -> bool:
        return self.debit is not None or self.credit is not None


@dataclass(frozen=True)
class Figure:
    value: Decimal | None
    source: str | None = None
    statement_id: int | None = None
    note: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {"value": money(self.value), "source": self.source, "statement_id": self.statement_id,
                "note": self.note}


@dataclass
class StatementData:
    statement_id: int
    document_id: int
    filename: str
    account_number: str | None
    bank_name: str | None
    ifsc: str | None
    printed_from: date | None
    printed_to: date | None
    period_from: date | None         # effective period (printed, widened or inferred); None: unusable
    period_to: date | None
    period_inferred: bool
    opening: Figure
    closing: Figure
    rows: list[Row]                  # chronological
    order: str
    validation: str | None           # validation outcome of the current extraction, if validated
    notes: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.period_from is not None and self.period_to is not None

    @property
    def unverified(self) -> bool:
        return self.validation == "failed"

    def to_json(self) -> dict[str, Any]:
        return {
            "statement_id": self.statement_id, "document_id": self.document_id, "filename": self.filename,
            "bank_name": self.bank_name,
            "printed_from": _iso(self.printed_from), "printed_to": _iso(self.printed_to),
            "period_from": _iso(self.period_from), "period_to": _iso(self.period_to),
            "period_inferred": self.period_inferred, "usable": self.usable,
            "opening": self.opening.to_json(), "closing": self.closing.to_json(),
            "transaction_count": len(self.rows), "row_order": self.order,
            "validation": self.validation, "unverified": self.unverified, "notes": list(self.notes),
        }


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def prepare_statement(
    statement_id: int, document_id: int, filename: str, header: dict[str, Any], rows: list[dict[str, Any]],
    validation: str | None, row_ids: list[int] | None = None,
) -> StatementData:
    """A statement's reviewed fields, read for reconciliation. Only usable values are read (a value the
    reviewer has not fixed counts as not printed); nothing is changed."""
    opening_value = amount(header, "opening_balance")
    numbered, order = chronological_rows(rows, opening_value)
    ids = row_ids or [None] * len(rows)
    parsed = [Row(n, day(r, "txn_date"), amount(r, "debit"), amount(r, "credit"), amount(r, "balance"),
                  ids[n - 1], text(r, "description"), text(r, "reference_number") or text(r, "cheque_number"))
              for n, r in numbered]
    notes: list[str] = []
    printed_from, printed_to = day(header, "period_from"), day(header, "period_to")
    dated = [r.txn_date for r in parsed if r.txn_date is not None]
    period_from, period_to, inferred = printed_from, printed_to, False

    if printed_from and printed_to and printed_from > printed_to:
        notes.append("The printed statement period starts after it ends; the statement cannot be placed in time.")
        period_from = period_to = None
    elif printed_from is None or printed_to is None:
        if dated:
            inferred = True
            period_from = printed_from or min(dated)
            period_to = printed_to or max(dated)
            notes.append(
                "The statement period is not printed in full; it was taken from the transaction dates "
                f"({period_from:%d %b %Y} – {period_to:%d %b %Y}). Days outside it are not counted as covered.")
        else:
            notes.append("No statement period and no dated transactions: the statement cannot be placed in time.")
            period_from = period_to = None
    if period_from and period_to and dated and (min(dated) < period_from or max(dated) > period_to):
        period_from, period_to = min(period_from, min(dated)), max(period_to, max(dated))
        notes.append("Some transactions are dated outside the printed period; the period was widened to include them.")
    if order == "mixed":
        notes.append("Transactions are not in date order: balances inside the statement cannot be read by date.")
    undated = sum(1 for r in parsed if r.txn_date is None)
    if undated:
        notes.append(f"{undated} transaction(s) have no usable date.")
    missing_amounts = sum(1 for r in parsed if not r.amount_known)
    if missing_amounts:
        notes.append(f"{missing_amounts} transaction(s) have no usable debit or credit amount.")
    if validation == "failed":
        notes.append("Validation of this statement failed: its figures are unverified.")
    elif validation is None:
        notes.append("This statement has not been validated.")

    opening = Figure(opening_value, "statement_opening", statement_id) if opening_value is not None else None
    if opening is None:
        first = parsed[0] if parsed else None
        if first is not None and order != "mixed" and first.balance is not None and first.amount_known:
            value = first.balance - (first.credit or ZERO) + (first.debit or ZERO)
            opening = Figure(value, "derived_first_row", statement_id,
                             "No opening balance printed: first transaction's balance − its credit + its debit.")
        else:
            opening = Figure(None, None, statement_id, "No opening balance printed, and it cannot be read from the first transaction.")
    closing_value = amount(header, "closing_balance")
    if closing_value is not None:
        closing = Figure(closing_value, "statement_closing", statement_id)
    elif parsed and order != "mixed" and parsed[-1].balance is not None:
        closing = Figure(parsed[-1].balance, "last_row_balance", statement_id,
                         "No closing balance printed: balance after the last transaction.")
    else:
        closing = Figure(None, None, statement_id, "No closing balance printed, and the last transaction shows no balance.")

    return StatementData(
        statement_id=statement_id, document_id=document_id, filename=filename,
        account_number=text(header, "account_number"), bank_name=text(header, "bank_name"), ifsc=text(header, "ifsc"),
        printed_from=printed_from, printed_to=printed_to, period_from=period_from, period_to=period_to,
        period_inferred=inferred, opening=opening, closing=closing, rows=parsed, order=order,
        validation=validation, notes=notes,
    )


# ---- Balances inside one statement ------------------------------------------------------------------

def _single_window(stmt: StatementData, start: date, end: date) -> bool:
    return start == stmt.period_from and end == stmt.period_to


def balance_at_end_of(stmt: StatementData, when: date) -> Figure:
    """The account's balance at the end of ``when`` (inside the statement's period), as the statement shows it."""
    assert stmt.period_from and stmt.period_to and stmt.period_from <= when <= stmt.period_to
    if when == stmt.period_to:
        return stmt.closing
    if stmt.order == "mixed":
        return Figure(None, None, stmt.statement_id, "Transactions are not in date order: the balance on this day cannot be read.")
    if any(r.txn_date is None for r in stmt.rows):
        return Figure(None, None, stmt.statement_id, "Some transactions have no date: the balance on this day cannot be read.")
    before = [r for r in stmt.rows if r.txn_date <= when]
    if not before:
        if stmt.opening.value is None:
            return Figure(None, None, stmt.statement_id, "No transactions before this day and no opening balance.")
        return Figure(stmt.opening.value, stmt.opening.source, stmt.statement_id,
                      "No transactions since the statement's opening balance.")
    last = before[-1]
    if last.balance is None:
        return Figure(None, None, stmt.statement_id,
                      f"Transaction {last.number} (the last on or before {when:%d %b %Y}) shows no balance.")
    return Figure(last.balance, "row_balance", stmt.statement_id,
                  f"Balance printed after transaction {last.number} ({last.txn_date:%d %b %Y}).")


def balance_at_start_of(stmt: StatementData, when: date) -> Figure:
    assert stmt.period_from
    if when == stmt.period_from:
        return stmt.opening
    return balance_at_end_of(stmt, day_before(when))


@dataclass
class Segment:
    """The part of one statement that falls in a window (a month)."""

    statement_id: int
    document_id: int
    start: date
    end: date
    opening: Figure
    closing: Figure
    credits: Decimal | None
    debits: Decimal | None
    transaction_count: int
    notes: list[str] = field(default_factory=list)

    def figures(self) -> tuple:
        return self.opening.value, self.closing.value, self.credits, self.debits

    def to_json(self) -> dict[str, Any]:
        return {
            "statement_id": self.statement_id, "document_id": self.document_id,
            "from": self.start.isoformat(), "to": self.end.isoformat(),
            "opening": self.opening.to_json(), "closing": self.closing.to_json(),
            "total_credits": money(self.credits), "total_debits": money(self.debits),
            "transaction_count": self.transaction_count, "notes": list(self.notes),
        }


def segment(stmt: StatementData, start: date, end: date) -> Segment:
    whole = _single_window(stmt, start, end)
    rows = [r for r in stmt.rows if (r.txn_date is None and whole) or (r.txn_date is not None and start <= r.txn_date <= end)]
    notes: list[str] = []
    credits: Decimal | None = sum((r.credit or ZERO for r in rows), ZERO)
    debits: Decimal | None = sum((r.debit or ZERO for r in rows), ZERO)
    if not whole and any(r.txn_date is None for r in stmt.rows):
        credits = debits = None
        notes.append("Some transactions of this statement have no date: totals for part of it cannot be worked out.")
    elif any(not r.amount_known for r in rows):
        credits = debits = None
        notes.append("Some transactions have no usable amount: totals cannot be worked out.")
    return Segment(stmt.statement_id, stmt.document_id, start, end, balance_at_start_of(stmt, start), balance_at_end_of(stmt, end),
                   credits, debits, len(rows), notes)


# ---- Account timeline ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Piece:
    statement: StatementData
    start: date
    end: date


@dataclass
class Overlap:
    first: StatementData
    second: StatementData
    start: date
    end: date
    agree: bool
    reason: str

    def to_json(self) -> dict[str, Any]:
        return {"statement_ids": [self.first.statement_id, self.second.statement_id],
                "document_ids": [self.first.document_id, self.second.document_id],
                "from": self.start.isoformat(), "to": self.end.isoformat(), "agree": self.agree, "reason": self.reason}


@dataclass
class Timeline:
    statements: list[StatementData]          # usable ones
    unusable: list[StatementData]
    pieces: list[Piece]                      # non-overlapping, in date order
    overlaps: list[Overlap]

    def document_of(self, statement_id: int | None) -> int | None:
        return next((s.document_id for s in self.statements + self.unusable if s.statement_id == statement_id), None)

    @property
    def first_day(self) -> date | None:
        return self.pieces[0].start if self.pieces else None

    @property
    def last_day(self) -> date | None:
        return max(p.end for p in self.pieces) if self.pieces else None


def _compare_overlap(a: StatementData, b: StatementData, start: date, end: date) -> Overlap:
    first, second = segment(a, start, end), segment(b, start, end)
    if any(v is None for v in first.figures() + second.figures()):
        return Overlap(a, b, start, end, False,
                       "Both statements cover these days, but their figures cannot both be read, so they cannot be confirmed to agree.")
    if first.figures() != second.figures():
        return Overlap(a, b, start, end, False, "Both statements cover these days with different balances or totals.")
    return Overlap(a, b, start, end, True, "Both statements cover these days and agree.")


def build_timeline(statements: list[StatementData]) -> Timeline:
    """Arrange an account's statements in date order. Each day is taken from the earliest statement that
    covers it; where statements overlap, they must agree (same opening, closing and totals for the shared
    days), or the months concerned are in conflict."""
    usable = sorted((s for s in statements if s.usable), key=lambda s: (s.period_from, -s.period_to.toordinal(), s.statement_id))
    unusable = [s for s in statements if not s.usable]
    pieces: list[Piece] = []
    overlaps: list[Overlap] = []
    covered_until: date | None = None
    for index, stmt in enumerate(usable):
        for earlier in usable[:index]:
            if earlier.period_to >= stmt.period_from:
                overlaps.append(_compare_overlap(earlier, stmt, stmt.period_from, min(stmt.period_to, earlier.period_to)))
        start = stmt.period_from if covered_until is None else max(stmt.period_from, day_after(covered_until))
        if start <= stmt.period_to:
            pieces.append(Piece(stmt, start, stmt.period_to))
        covered_until = stmt.period_to if covered_until is None else max(covered_until, stmt.period_to)
    return Timeline(usable, unusable, pieces, overlaps)


def covered_ranges(timeline: Timeline, start: date, end: date) -> list[tuple[date, date]]:
    out: list[tuple[date, date]] = []
    for piece in timeline.pieces:
        a, b = max(piece.start, start), min(piece.end, end)
        if a > b:
            continue
        if out and day_after(out[-1][1]) >= a:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def gaps(timeline: Timeline, start: date, end: date) -> list[tuple[date, date]]:
    """Days in [start, end] that no statement covers."""
    out, cursor = [], start
    for a, b in covered_ranges(timeline, start, end):
        if a > cursor:
            out.append((cursor, day_before(a)))
        cursor = max(cursor, day_after(b))
    if cursor <= end:
        out.append((cursor, end))
    return out


# ---- Periods (months and financial years) ----------------------------------------------------------

@dataclass
class Link:
    """This period's opening balance against the previous period's closing balance."""

    status: str                     # matched / mismatch / unknown
    previous_closing: Figure
    opening: Figure
    difference: Decimal | None
    same_statement: bool

    def to_json(self) -> dict[str, Any]:
        return {"status": self.status, "previous_closing": self.previous_closing.to_json(),
                "opening": self.opening.to_json(), "difference": money(self.difference),
                "same_statement": self.same_statement}


def _link(previous_closing: Figure, opening: Figure) -> Link:
    same = (previous_closing.statement_id is not None and previous_closing.statement_id == opening.statement_id)
    if previous_closing.value is None or opening.value is None:
        return Link(UNKNOWN, previous_closing, opening, None, same)
    difference = opening.value - previous_closing.value
    return Link(MATCHED if difference == 0 else MISMATCH, previous_closing, opening, difference, same)


@dataclass
class PeriodResult:
    label: str
    start: date
    end: date
    status: str
    opening: Figure
    closing: Figure
    credits: Decimal | None
    debits: Decimal | None
    calculated_closing: Decimal | None
    difference: Decimal | None          # closing − calculated closing
    previous: Link | None = None
    missing_ranges: list[tuple[date, date]] = field(default_factory=list)
    breaks: list[dict[str, Any]] = field(default_factory=list)       # balance jumps between statements inside the period
    conflicts: list[Overlap] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)
    statement_ids: list[int] = field(default_factory=list)
    document_ids: list[int] = field(default_factory=list)
    unverified: bool = False
    inferred: bool = False
    notes: list[str] = field(default_factory=list)
    missing_months: list[str] = field(default_factory=list)          # years only
    month_statuses: dict[str, int] = field(default_factory=dict)     # years only

    def to_json(self) -> dict[str, Any]:
        return {
            "label": self.label, "from": self.start.isoformat(), "to": self.end.isoformat(), "status": self.status,
            "opening": self.opening.to_json(), "closing": self.closing.to_json(),
            "total_credits": money(self.credits), "total_debits": money(self.debits),
            "calculated_closing": money(self.calculated_closing), "difference": money(self.difference),
            "previous": self.previous.to_json() if self.previous else None,
            "missing_ranges": [{"from": a.isoformat(), "to": b.isoformat()} for a, b in self.missing_ranges],
            "breaks": self.breaks, "conflicts": [c.to_json() for c in self.conflicts],
            "segments": [s.to_json() for s in self.segments], "statement_ids": self.statement_ids,
            "document_ids": self.document_ids,
            "unverified": self.unverified, "inferred": self.inferred, "notes": self.notes,
            "missing_months": self.missing_months, "month_statuses": self.month_statuses,
        }


def _unknown(note: str) -> Figure:
    return Figure(None, None, None, note)


def _calculated(opening: Figure, credits: Decimal | None, debits: Decimal | None, closing: Figure
                ) -> tuple[Decimal | None, Decimal | None]:
    if opening.value is None or credits is None or debits is None:
        return None, None
    calculated = opening.value + credits - debits
    return calculated, (closing.value - calculated if closing.value is not None else None)


def month_result(timeline: Timeline, month: Month) -> PeriodResult:
    start, end = month_bounds(month)
    label = month_label(month)
    pieces = [p for p in timeline.pieces if p.start <= end and p.end >= start]
    segments = [segment(p.statement, max(p.start, start), min(p.end, end)) for p in pieces]
    missing = gaps(timeline, start, end)
    conflicts = [o for o in timeline.overlaps if not o.agree and o.start <= end and o.end >= start]
    statements = {p.statement.statement_id: p.statement for p in pieces}
    for o in timeline.overlaps:
        if o.start <= end and o.end >= start:
            statements.setdefault(o.second.statement_id, o.second)
    notes = [f"Documents #{o.first.document_id} and #{o.second.document_id}: {o.reason}"
             for o in timeline.overlaps if o.start <= end and o.end >= start]

    if not segments:
        return PeriodResult(label, start, end, MISSING, _unknown("No statement covers this month."),
                            _unknown("No statement covers this month."), None, None, None, None,
                            missing_ranges=missing, conflicts=conflicts, notes=notes)

    opening = segments[0].opening if segments[0].start == start else _unknown(
        f"No statement covers {start:%d %b %Y}: the opening balance is not known.")
    closing = segments[-1].closing if segments[-1].end == end else _unknown(
        f"No statement covers {end:%d %b %Y}: the closing balance is not known.")
    breaks = []
    for before, after in zip(segments, segments[1:]):
        if day_after(before.end) != after.start:
            continue
        link = _link(before.closing, after.opening)
        if link.status != MATCHED:
            breaks.append({"on": after.start.isoformat(), "status": link.status, "difference": money(link.difference),
                           "previous_document_id": before.document_id, "next_document_id": after.document_id,
                           "previous_closing": money(before.closing.value), "next_opening": money(after.opening.value)})
    complete = not missing and all(s.credits is not None for s in segments)
    credits = sum((s.credits for s in segments), ZERO) if complete else None
    debits = sum((s.debits for s in segments), ZERO) if complete else None
    calculated, difference = _calculated(opening, credits, debits, closing)
    for s in segments:
        notes += s.notes

    if conflicts:
        status = CONFLICT
    elif missing:
        status = PARTIAL
    elif calculated is None or closing.value is None:
        status = INCOMPLETE
    elif difference != 0 or any(b["status"] == MISMATCH for b in breaks):
        status = MISMATCH
    elif breaks:
        status = INCOMPLETE
    else:
        status = MATCHED
    return PeriodResult(
        label, start, end, status, opening, closing, credits, debits, calculated, difference,
        missing_ranges=missing, breaks=breaks, conflicts=conflicts, segments=segments,
        statement_ids=sorted(statements), document_ids=sorted({s.document_id for s in statements.values()}),
        unverified=any(s.unverified for s in statements.values()),
        inferred=any(s.period_inferred for s in statements.values()), notes=list(dict.fromkeys(notes)),
    )


MAX_MONTHS = 120
MAX_YEARS = 20


def monthly(timeline: Timeline, first: Month, last: Month) -> list[PeriodResult]:
    """Each month from ``first`` to ``last``, linked to the month before (also for ``first``)."""
    months = list(months_between(first, last))
    if not months:
        raise ValueError("The first month is after the last month.")
    if len(months) > MAX_MONTHS:
        raise ValueError(f"At most {MAX_MONTHS} months at a time.")
    previous_month = (first[0] - 1, 12) if first[1] == 1 else (first[0], first[1] - 1)
    previous = month_result(timeline, previous_month)
    out = []
    for month in months:
        result = month_result(timeline, month)
        result.previous = _link(previous.closing, result.opening)
        out.append(result)
        previous = result
    return out


def year_result(timeline: Timeline, start_year: int) -> PeriodResult:
    start, end = fy_bounds(start_year)
    months = [month_result(timeline, m) for m in fy_months(start_year)]
    covered = [m for m in months if m.status != MISSING]
    missing_months = [m.label for m in months if m.status == MISSING]
    missing = gaps(timeline, start, end)
    conflicts = list({id(c): c for m in months for c in m.conflicts}.values())
    statuses: dict[str, int] = {}
    for m in months:
        statuses[m.status] = statuses.get(m.status, 0) + 1
    statement_ids = sorted({i for m in months for i in m.statement_ids})
    document_ids = sorted({i for m in months for i in m.document_ids})
    breaks = [b for m in months for b in m.breaks]
    # Breaks between months (a month's opening against the previous month's closing, from different statements).
    for before, after in zip(months, months[1:]):
        link = _link(before.closing, after.opening)
        if link.status == MISMATCH and not link.same_statement:
            breaks.append({"on": after.start.isoformat(), "status": MISMATCH, "difference": money(link.difference),
                           "previous_document_id": timeline.document_of(link.previous_closing.statement_id),
                           "next_document_id": timeline.document_of(link.opening.statement_id),
                           "previous_closing": money(link.previous_closing.value), "next_opening": money(link.opening.value)})
    opening = months[0].opening if months[0].status != MISSING else _unknown("No statement covers April.")
    closing = months[-1].closing if months[-1].status != MISSING else _unknown("No statement covers March.")
    complete = not missing and all(m.credits is not None for m in months)
    credits = sum((m.credits for m in months), ZERO) if complete else None
    debits = sum((m.debits for m in months), ZERO) if complete else None
    calculated, difference = _calculated(opening, credits, debits, closing)
    notes = list(dict.fromkeys(n for m in months for n in m.notes))

    if not covered:
        status = MISSING
    elif conflicts:
        status = CONFLICT
    elif missing:
        status = PARTIAL
    elif calculated is None or closing.value is None:
        status = INCOMPLETE
    elif difference != 0 or any(b["status"] == MISMATCH for b in breaks) or statuses.get(MISMATCH):
        status = MISMATCH
    elif statuses.get(INCOMPLETE) or breaks:
        status = INCOMPLETE
    else:
        status = MATCHED
    return PeriodResult(
        fy_label(start_year), start, end, status, opening, closing, credits, debits, calculated, difference,
        missing_ranges=missing, breaks=breaks, conflicts=conflicts, statement_ids=statement_ids, document_ids=document_ids,
        unverified=any(m.unverified for m in months), inferred=any(m.inferred for m in months), notes=notes,
        missing_months=missing_months, month_statuses=statuses,
    )


def yearly(timeline: Timeline, first_year: int, last_year: int) -> list[PeriodResult]:
    """Each financial year, linked to the year before: its closing on 31 March against this year's
    opening on 1 April."""
    if first_year > last_year:
        raise ValueError("The first financial year is after the last one.")
    if last_year - first_year + 1 > MAX_YEARS:
        raise ValueError(f"At most {MAX_YEARS} financial years at a time.")
    march_before = month_result(timeline, (first_year, 3))
    previous_closing = march_before.closing
    out = []
    for start_year in range(first_year, last_year + 1):
        result = year_result(timeline, start_year)
        result.previous = _link(previous_closing, result.opening)
        out.append(result)
        previous_closing = result.closing
    return out


def summarize(results: list[PeriodResult]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    counts["opening_mismatches"] = sum(1 for r in results if r.previous and r.previous.status == MISMATCH)
    return counts


def default_months(timeline: Timeline) -> tuple[Month, Month] | None:
    if timeline.first_day is None:
        return None
    return month_of(timeline.first_day), month_of(timeline.last_day)
