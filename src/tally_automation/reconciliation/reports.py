"""Reconciliation reports (Excel and PDF) from a saved run.

Both formats are written from the same tables, built from the run as saved (with the match decisions current
at download time), so they always show the same figures. Account numbers are masked. Amounts stay exact:
Excel cells receive the ``Decimal`` itself; text is always stored as text, so a narration starting with
"=" can never become a formula.
"""

from __future__ import annotations

import html
import io
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import fitz  # PyMuPDF
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

MONEY = "money"
TEXT = "text"
NUMBER = "number"
EXCEL_MONEY_FORMAT = "#,##0.00;[Red]-#,##0.00"

STATUS_TEXT = {
    "matched": "Matched", "mismatch": "Mismatch", "missing": "Missing statement", "partial": "Partly covered",
    "incomplete": "Figures incomplete", "conflict": "Statements disagree", "unknown": "Cannot compare",
}
MATCH_TEXT = {"matched": "Matched", "uncertain": "Uncertain", "partial": "Partly matched",
              "duplicate": "Duplicate in TallyPrime"}
SOURCE_TEXT = {
    "statement_opening": "printed opening", "statement_closing": "printed closing", "row_balance": "running balance",
    "last_row_balance": "balance after last transaction", "derived_first_row": "from first transaction",
}


@dataclass
class Table:
    title: str
    columns: list[tuple[str, str]]               # (heading, kind)
    rows: list[list[Any]] = field(default_factory=list)
    note: str | None = None


@dataclass
class Report:
    title: str
    summary: list[tuple[str, Any, str]]          # (label, value, kind)
    tables: list[Table]


def _dec(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _source(figure: dict[str, Any]) -> str:
    if figure["value"] is None:
        return figure.get("note") or "not known"
    return SOURCE_TEXT.get(figure.get("source"), figure.get("source") or "")


# ---- Building the tables ----------------------------------------------------------------------------

def build_report(meta: dict[str, Any], run: dict[str, Any]) -> Report:
    summary: list[tuple[str, Any, str]] = [
        ("Account", meta["account_number"], TEXT),
        ("Bank", ", ".join(meta["bank_names"]) or "—", TEXT),
        ("Period", f"{meta['period_from']} to {meta['period_to']}", TEXT),
        ("Reconciliation", meta["kind_label"], TEXT),
        ("Saved", f"{meta['created_at']} by {meta['created_by'] or 'unknown'}", TEXT),
        ("Report made", meta["generated_at"], TEXT),
    ]
    if meta.get("ledger_name"):
        summary.append(("TallyPrime ledger", f"{meta['ledger_name']} ({meta.get('company_name') or ''})", TEXT))
    if meta.get("stale"):
        summary.append(("Warning", "The statements have been corrected since this reconciliation was saved; "
                                   "save it again for current figures.", TEXT))
    if run.get("kind") == "transactions":
        return _transactions_report(meta, run, summary)
    return _balances_report(meta, run, summary)


def _balances_report(meta: dict[str, Any], run: dict[str, Any], summary: list) -> Report:
    unit = "Months" if run["kind"] == "monthly" else "Financial years"
    counts = run["summary"]
    for status in ("matched", "mismatch", "missing", "partial", "incomplete", "conflict"):
        summary.append((f"{unit}: {STATUS_TEXT[status].lower()}", counts.get(status, 0), NUMBER))
    summary.append(("Openings different from the previous closing", counts.get("opening_mismatches", 0), NUMBER))
    summary.append(("Formula", "Calculated closing = opening + total credits − total debits; "
                               "difference = closing − calculated closing. Blank = not shown by the statements "
                               "(never zero).", TEXT))
    periods = Table(f"{unit}", [
        ("Period", TEXT), ("Opening", MONEY), ("Opening from", TEXT), ("Credits", MONEY), ("Debits", MONEY),
        ("Calculated closing", MONEY), ("Closing", MONEY), ("Closing from", TEXT), ("Difference", MONEY),
        ("Previous closing", MONEY), ("Opening − previous closing", MONEY), ("Status", TEXT), ("Notes", TEXT),
    ])
    for p in run["periods"]:
        link = p["previous"] or {}
        notes = [f"Not covered: {r['from']} to {r['to']}" for r in p["missing_ranges"]]
        notes += [f"Jump on {b['on']}: document #{b['previous_document_id']} closes {b['previous_closing']}, "
                  f"document #{b['next_document_id']} opens {b['next_opening']}" for b in p["breaks"]]
        notes += [f"Documents #{' and #'.join(map(str, c['document_ids']))}: {c['reason']}" for c in p["conflicts"]]
        if p["unverified"]:
            notes.append("Includes an unverified statement (validation failed).")
        periods.rows.append([
            p["label"], _dec(p["opening"]["value"]), _source(p["opening"]), _dec(p["total_credits"]),
            _dec(p["total_debits"]), _dec(p["calculated_closing"]), _dec(p["closing"]["value"]), _source(p["closing"]),
            _dec(p["difference"]), _dec((link.get("previous_closing") or {}).get("value")), _dec(link.get("difference")),
            STATUS_TEXT.get(p["status"], p["status"]), "; ".join(notes),
        ])
    cover = run["coverage"]
    missing = Table("Missing statements", [("What", TEXT), ("Period", TEXT), ("Days missing", NUMBER)],
                    note="Missing periods are reported as missing; no balance is assumed for them.")
    for r in cover["missing"]:
        missing.rows.append(["Gap between statements", f"{r['from']} to {r['to']}", r["days"]])
    for m in cover["missing_months"]:
        missing.rows.append(["Month without a statement", m, None])
    for m in cover["partial_months"]:
        missing.rows.append(["Month covered in part", m["month"], m["missing_days"]])
    for y in cover["missing_years"]:
        missing.rows.append(["Financial year without a statement", y, None])
    for y in cover["partial_years"]:
        missing.rows.append(["Financial year covered in part", y["year"], y["missing_days"]])
    statements = Table("Statements used", [
        ("Document", NUMBER), ("File", TEXT), ("From", TEXT), ("To", TEXT), ("Opening", MONEY), ("Closing", MONEY),
        ("Transactions", NUMBER), ("Validation", TEXT), ("Notes", TEXT),
    ])
    for s in run.get("statements", []):
        statements.rows.append([s["document_id"], s["filename"], s["period_from"], s["period_to"],
                                _dec(s["opening"]["value"]), _dec(s["closing"]["value"]), s["transaction_count"],
                                s["validation"] or "not validated", " ".join(s["notes"])])
    return Report(f"Bank reconciliation — {meta['label']}", summary, [periods, missing, statements])


def _transactions_report(meta: dict[str, Any], run: dict[str, Any], summary: list) -> Report:
    c, st = run["counts"], run["statement"]
    summary += [
        ("Matched exactly", c["matched"], NUMBER), ("Approved by a person", c["approved"], NUMBER),
        ("Awaiting approval", c["awaiting_approval"], NUMBER), ("Rejected", c["rejected"], NUMBER),
        ("Duplicates in TallyPrime", c["duplicate"], NUMBER), ("Only in the statement", c["bank_only"], NUMBER),
        ("Only in TallyPrime", c["tally_only"], NUMBER), ("Not compared", c["excluded"] + c["unreadable"], NUMBER),
    ]
    brs = Table("Bank reconciliation statement", [("", TEXT), ("Amount", MONEY)],
                note="Amounts from the bank account's side: money in positive, money out negative. "
                     "Blank = not known. Awaiting approval counts as matched until rejected.")
    brs.rows += [
        ["Balance per TallyPrime at the start", _dec(st["tally_opening"])],
        ["Balance per statement at the start", _dec(st["bank_opening"])],
        ["Difference brought forward", _dec(st["brought_forward"])],
        ["Balance per TallyPrime at the end", _dec(st["tally_closing"])],
        ["+ Transactions only in the statement", _dec(st["bank_only_total"])],
        ["− Entries only in TallyPrime (incl. duplicates)", _dec(st["tally_only_total"])],
        ["+ Differences inside matches", _dec(st["match_differences"])],
        ["= Expected balance per statement at the end", _dec(st["expected_bank_closing"])],
        ["Balance per statement at the end", _dec(st["bank_closing"])],
        ["Unexplained difference", _dec(st["unexplained"])],
    ]
    for note in run.get("tally_notes", []):
        brs.rows.append([note, None])

    matches = Table("Matches", [
        ("Status", TEXT), ("Decision", TEXT), ("Reason", TEXT), ("Statement date", TEXT), ("Document / row", TEXT),
        ("Narration", TEXT), ("Statement amount", MONEY), ("TallyPrime date", TEXT), ("Voucher", TEXT),
        ("TallyPrime narration", TEXT), ("TallyPrime amount", MONEY), ("Difference", MONEY),
    ])
    for m in run["matches"]:
        decision = m.get("decision")
        decided = (f"{decision['decision']} by {decision['by'] or 'unknown'}"
                   + (f": {decision['note']}" if decision.get("note") else "")) if decision else (
            "awaiting approval" if m["needs_approval"] else "")
        matches.rows.append([
            MATCH_TEXT.get(m["status"], m["status"]), decided, m["reason"],
            ", ".join(b["date"] for b in m["bank"]), ", ".join(f"#{b['document_id']}/{b['row']}" for b in m["bank"]),
            " | ".join(b["description"] or "" for b in m["bank"]),
            sum((Decimal(b["signed"]) for b in m["bank"]), Decimal("0")) if m["bank"] else None,
            ", ".join(t["date"] for t in m["tally"]),
            ", ".join(f"{t['voucher_type'] or ''} {t['number'] or ''}".strip() for t in m["tally"]),
            " | ".join(t["narration"] or "" for t in m["tally"]),
            sum((Decimal(t["signed"]) for t in m["tally"]), Decimal("0")) if m["tally"] else None,
            Decimal(m["difference"]),
        ])
    bank_only = Table("Only in the statement", [("Date", TEXT), ("Document / row", TEXT), ("Narration", TEXT),
                                                ("Reference", TEXT), ("Amount", MONEY), ("Note", TEXT)])
    for b in run["bank_only"]:
        bank_only.rows.append([b["date"], f"#{b['document_id']}/{b['row']}", b["description"], b["reference"],
                               Decimal(b["signed"]), "from a rejected match" if b.get("from_rejected_match") else ""])
    tally_only = Table("Only in TallyPrime", [("Date", TEXT), ("Voucher", TEXT), ("Narration", TEXT),
                                             ("Amount", MONEY), ("Note", TEXT)])
    for t in run["tally_only"]:
        tally_only.rows.append([t["date"], f"{t['voucher_type'] or ''} {t['number'] or ''}".strip(), t["narration"],
                                Decimal(t["signed"]), "from a rejected match" if t.get("from_rejected_match") else ""])
    not_compared = Table("Not compared", [("Date", TEXT), ("Document / row", TEXT), ("Narration", TEXT), ("Why", TEXT)])
    for item in run["excluded"] + run["unreadable"]:
        not_compared.rows.append([item.get("date") or "", f"#{item['document_id']}/{item['row']}",
                                  item.get("description"), item["reason"]])
    return Report(f"Bank reconciliation with TallyPrime — {meta['label']}", summary,
                  [brs, matches, bank_only, tally_only, not_compared])


# ---- Excel ------------------------------------------------------------------------------------------

def _safe_title(title: str, used: set[str]) -> str:
    cleaned = "".join(ch for ch in title if ch not in '[]:*?/\\')[:31] or "Sheet"
    name, n = cleaned, 2
    while name in used:
        name = f"{cleaned[:28]} {n}"
        n += 1
    used.add(name)
    return name


def _put(cell, value: Any, kind: str) -> None:
    if value is None:
        return
    if kind == MONEY and isinstance(value, Decimal):
        cell.value = value
        cell.number_format = EXCEL_MONEY_FORMAT
    elif kind == NUMBER and isinstance(value, (int, Decimal)):
        cell.value = value
    else:
        cell.value = str(value)
        cell.data_type = "s"          # text stays text: never a formula


def to_xlsx(report: Report) -> bytes:
    workbook = Workbook()
    used: set[str] = set()
    bold = Font(bold=True)
    head_fill = PatternFill("solid", fgColor="E8EEF9")
    sheet = workbook.active
    sheet.title = _safe_title("Summary", used)
    _put(sheet.cell(1, 1), report.title, TEXT)
    sheet.cell(1, 1).font = Font(bold=True, size=13)
    for index, (label, value, kind) in enumerate(report.summary, start=3):
        _put(sheet.cell(index, 1), label, TEXT)
        sheet.cell(index, 1).font = bold
        _put(sheet.cell(index, 2), value, kind)
        sheet.cell(index, 2).alignment = Alignment(wrap_text=True, vertical="top")
    sheet.column_dimensions["A"].width = 42
    sheet.column_dimensions["B"].width = 90
    for table in report.tables:
        ws = workbook.create_sheet(_safe_title(table.title, used))
        row = 1
        if table.note:
            _put(ws.cell(1, 1), table.note, TEXT)
            row = 3
        for col, (heading, _) in enumerate(table.columns, start=1):
            _put(ws.cell(row, col), heading, TEXT)
            ws.cell(row, col).font = bold
            ws.cell(row, col).fill = head_fill
        ws.freeze_panes = ws.cell(row + 1, 1)
        for values in table.rows:
            row += 1
            for col, ((_, kind), value) in enumerate(zip(table.columns, values), start=1):
                _put(ws.cell(row, col), value, kind)
        for col, (heading, kind) in enumerate(table.columns, start=1):
            width = 16 if kind != TEXT else min(60, max(12, len(heading) + 2, *(
                len(str(v[col - 1] or "")) for v in table.rows[:200])))
            ws.column_dimensions[get_column_letter(col)].width = width
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# ---- PDF --------------------------------------------------------------------------------------------

def indian(value: Decimal) -> str:
    """1234567.8 → '12,34,567.80' (lakh grouping)."""
    sign = "-" if value < 0 else ""
    whole, _, fraction = f"{abs(value):.2f}".partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{sign}{whole}.{fraction}"


# The PDF's built-in font covers Latin-1 only: typographic symbols are written plainly. Other scripts
# (e.g. Devanagari narrations) cannot be shown in the PDF; the Excel file keeps them as they are.
_PLAIN = str.maketrans({"—": "-", "–": "-", "−": "-", "‘": "'", "’": "'", "“": '"',
                        "”": '"', "…": "...", "↔": "<->", "≠": "!=", "₹": "Rs. ",
                        " ": " "})


def _plain(value: str) -> str:
    text = value.translate(_PLAIN)
    return "".join(ch if ord(ch) < 256 else "?" for ch in text)


def _esc(value: Any) -> str:
    return html.escape(_plain(str(value)))


def _cell_html(value: Any, kind: str) -> str:
    if value is None:
        return "<td></td>"
    if kind == MONEY and isinstance(value, Decimal):
        return f'<td class="num">{indian(value)}</td>'
    if kind == NUMBER:
        return f'<td class="num">{_esc(value)}</td>'
    return f"<td>{_esc(value)}</td>"


_CSS = """
* { font-family: sans-serif; }
body { font-size: 8pt; }
h1 { font-size: 13pt; margin: 0 0 6pt 0; }
h2 { font-size: 10pt; margin: 12pt 0 4pt 0; }
table { border-collapse: collapse; width: 100%; }
th, td { border: 0.5pt solid #999; padding: 2pt 3pt; vertical-align: top; text-align: left; }
th { background-color: #e8eef9; }
td.num { text-align: right; }
p.note { color: #444; margin: 2pt 0; }
"""


def to_pdf(report: Report) -> bytes:
    parts = [f"<h1>{_esc(report.title)}</h1><p class='note'>Amounts in INR.</p><table>"]
    for label, value, kind in report.summary:
        parts.append(f"<tr><th>{_esc(label)}</th>{_cell_html(value, kind)}</tr>")
    parts.append("</table>")
    for table in report.tables:
        parts.append(f"<h2>{_esc(table.title)}</h2>")
        if table.note:
            parts.append(f"<p class='note'>{_esc(table.note)}</p>")
        if not table.rows:
            parts.append("<p class='note'>None.</p>")
            continue
        parts.append("<table><tr>" + "".join(f"<th>{_esc(h)}</th>" for h, _ in table.columns) + "</tr>")
        for values in table.rows:
            parts.append("<tr>" + "".join(_cell_html(v, k) for (_, k), v in zip(table.columns, values)) + "</tr>")
        parts.append("</table>")
    story = fitz.Story(html="".join(parts), user_css=_CSS)
    buffer = io.BytesIO()
    writer = fitz.DocumentWriter(buffer)
    page = fitz.paper_rect("a4-l")
    area = page + (28, 28, -28, -28)
    more = True
    while more:
        device = writer.begin_page(page)
        more, _ = story.place(area)
        story.draw(device)
        writer.end_page()
    writer.close()
    return buffer.getvalue()
