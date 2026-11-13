"""Debit/credit amounts the LLM skipped or put in the wrong column, read from a ruled statement table.

PDF pages whose transactions sit in a table drawn with ruling lines reach the
LLM as one line per table row, cells separated by `` | `` (see
``processing.pdf_text``). When the model returns a transaction with neither a
debit nor a credit, but its row is such a table row and the table's header
names a debit and a credit column, the amount printed in that cell is taken.

This is transcription, not invention: the value is copied from the printed
cell. It is still marked for review (status ``warning``, method ``table``), so a
person confirms it before any voucher is generated. Nothing is filled when the
header is ambiguous, the cell is not a plain amount, or both cells hold one.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from tally_automation.db.models import DocumentType
from tally_automation.extraction.parsing import Status
from tally_automation.extraction.schemas import TRANSACTION_FIELDS
from tally_automation.extraction.service import (
    ExtractionResult, PageInput, RowResult, _DocumentText, build_field,
)

_SEPARATOR = " | "
_DEBIT_HEADER = re.compile(r"^(dr\.?|debit|withdrawals?)\b", re.IGNORECASE)
_CREDIT_HEADER = re.compile(r"^(cr\.?|credit|deposits?)\b", re.IGNORECASE)
_AMOUNT = re.compile(r"^\d{1,3}(?:,\d{2,3})*(?:\.\d{2})$|^\d+\.\d{2}$")
_SPECS = {spec.name: spec for spec in TRANSACTION_FIELDS}


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.split("|")]


def _amount_columns(pages: list[PageInput]) -> tuple[int, int] | None:
    """(debit index, credit index) from the first table header naming exactly one of each."""
    for page in pages:
        for line in page.text.splitlines():
            if _SEPARATOR not in line:
                continue
            cells = _cells(line)
            debit = [i for i, c in enumerate(cells) if _DEBIT_HEADER.match(c)]
            credit = [i for i, c in enumerate(cells) if _CREDIT_HEADER.match(c)]
            if len(debit) == 1 and len(credit) == 1 and debit != credit:
                return debit[0], credit[0]
    return None


def _find_row(pages: list[PageInput], row: RowResult) -> str | None:
    """The table line the model quoted as this row's text, exactly as it appears in the page text."""
    if not row.row_text or _SEPARATOR not in row.row_text:
        return None
    quoted = " ".join(row.row_text.split())
    for page in pages:
        if row.page is not None and page.page_number != row.page:
            continue
        for line in page.text.splitlines():
            if " ".join(line.split()).rstrip(" |") == quoted.rstrip(" |"):
                return line
    return None


# ---- Plain-text statements (no drawn table) ----------------------------------------------------
# The columns are kept as spaces: a header line names "WITHDRAWALS (DR)" and "DEPOSITS (CR)", and each
# amount sits under its column. When a narration wraps, some statements print the amount on the next line.

_LAYOUT_DEBIT = re.compile(r"WITHDRAWALS?\s*\(?\s*DR\s*\)?|WITHDRAWALS?|DEBITS?(?:\s+AMOUNT)?|DR\.?\s+AMOUNT|\(DR\)",
                           re.IGNORECASE)
_LAYOUT_CREDIT = re.compile(r"DEPOSITS?\s*\(?\s*CR\s*\)?|DEPOSITS?|CREDITS?(?:\s+AMOUNT)?|CR\.?\s+AMOUNT|\(CR\)",
                            re.IGNORECASE)
_LAYOUT_AMOUNT = re.compile(r"(?<![\d,.])\d{1,3}(?:,\d{2,3})*\.\d{2}(?![\d.])")
_DATE_START = re.compile(r"^\s*\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\b")
MAX_COLUMN_DISTANCE = 12
FOLLOWING_LINES = 3


def _layout_columns(pages: list[PageInput]) -> dict[int, tuple[int, int]]:
    """Per page: (end of the debit header, end of the credit header), from a header line naming one of
    each. A page without its own header uses the last one seen before it."""
    out: dict[int, tuple[int, int]] = {}
    last: tuple[int, int] | None = None
    for page in pages:
        for line in page.text.splitlines():
            if _SEPARATOR in line or "BALANCE" not in line.upper():
                continue
            debit, credit = list(_LAYOUT_DEBIT.finditer(line)), list(_LAYOUT_CREDIT.finditer(line))
            if len(debit) == 1 and len(credit) == 1 and debit[0].end() != credit[0].end():
                last = (debit[0].end(), credit[0].end())
                break
        if last is not None:
            out[page.page_number] = last
    return out


def _below_row(pages: list[PageInput], row: RowResult,
               columns: dict[int, tuple[int, int]]) -> tuple[str, str, str] | None:
    """(debit|credit, amount, its line) for a row whose amount is printed alone on a line just below it."""
    if not row.row_text or not row.row_text.strip():
        return None
    # The row's dated line: the model may also quote the wrapped lines below it.
    quoted = " ".join(row.row_text.strip().splitlines()[0].split())
    for page in pages:
        if row.page is not None and page.page_number != row.page or page.page_number not in columns:
            continue
        debit_end, credit_end = columns[page.page_number]
        lines = page.text.splitlines()
        for index, line in enumerate(lines):
            if " ".join(line.split()) != quoted:
                continue
            # Amounts under the debit or credit column: on the row's own line (its balance, further
            # right, is not near either column) and on the lines below it, until the next transaction.
            found = []
            for candidate in [line, *lines[index + 1:index + 1 + FOLLOWING_LINES]]:
                if candidate is not line and _DATE_START.match(candidate):
                    break
                for match in _LAYOUT_AMOUNT.finditer(candidate):
                    to_debit, to_credit = abs(match.end() - debit_end), abs(match.end() - credit_end)
                    if min(to_debit, to_credit) <= MAX_COLUMN_DISTANCE and to_debit != to_credit:
                        found.append(("debit" if to_debit < to_credit else "credit", match.group(), candidate))
            return found[0] if len(found) == 1 else None
    return None


def fill_skipped_amounts(
    result: ExtractionResult, pages: list[PageInput], *, low_confidence: float,
) -> int:
    """Fill debit or credit on statement rows where the model returned neither: from the row's debit or
    credit cell of a ruled table, or (a statement without a drawn table) from an amount printed alone
    just below the row, under its column. Returns the number of rows filled."""
    if result.document_type is not DocumentType.BANK_STATEMENT:
        return 0
    columns = _amount_columns(pages)
    layout = _layout_columns(pages) if columns is None else {}
    if columns is None and not layout:
        return 0
    document = _DocumentText(pages)
    filled = 0
    for row in result.rows:
        debit, credit = row.fields.get("debit"), row.fields.get("credit")
        if debit is None or credit is None or debit.raw is not None or credit.raw is not None:
            continue
        if columns is not None:
            line = _find_row(pages, row)
            if line is None:
                continue
            cells = _cells(line)
            found = {
                name: cells[index] for name, index in zip(("debit", "credit"), columns)
                if index < len(cells) and _AMOUNT.match(cells[index])
            }
            if len(found) != 1:
                continue
            name, raw = next(iter(found.items()))
            evidence, where = line.strip(), "column"
        else:
            below = _below_row(pages, row, layout)
            if below is None:
                continue
            name, raw, evidence = below[0], below[1], below[2].strip()
            where = "column, printed on the line below the row"
        field = build_field(
            _SPECS[name], raw, evidence=evidence, page=row.page, confidence=None,
            document=document, low_confidence=low_confidence, evidence_is_row=True,
        )
        if field.status is Status.INVALID:
            continue  # never keep a value the usual checks reject
        field.method = "table"
        field.flags.append("read_from_table")
        field.messages.append(
            f"The AI returned no amount for this row; this {name} was copied from the statement's "
            f"{name} {where}. Check it against the statement."
        )
        field.status = Status.WARNING
        row.fields[name] = field
        filled += 1
    return filled


def _number(text: str | None) -> Decimal | None:
    try:
        return Decimal(text.replace(",", "")) if text else None
    except InvalidOperation:
        return None


def correct_amount_columns(
    result: ExtractionResult, pages: list[PageInput], *, low_confidence: float,
) -> int:
    """Move an amount the model put in the wrong column (debit for credit, or the reverse).

    Only when the statement's own layout is clear: a ruled table row whose debit/credit header is clear
    and exactly one of the two cells holds an amount, or (no drawn table) exactly one amount printed under
    the debit or credit column on the row's line or just below it. When that printed amount equals the
    model's, the printed column is the truth: the value is moved there and the other side is emptied.
    Nothing new is ever invented. Returns the number of rows corrected.
    """
    if result.document_type is not DocumentType.BANK_STATEMENT:
        return 0
    columns = _amount_columns(pages)
    layout = _layout_columns(pages) if columns is None else {}
    if columns is None and not layout:
        return 0
    document = _DocumentText(pages)
    corrected = 0
    for row in result.rows:
        debit, credit = row.fields.get("debit"), row.fields.get("credit")
        if debit is None or credit is None or (debit.raw is None and credit.raw is None):
            continue
        if columns is not None:
            line = _find_row(pages, row)
            if line is None:
                continue
            cells = _cells(line)
            found = {
                name: cells[index] for name, index in zip(("debit", "credit"), columns)
                if index < len(cells) and _AMOUNT.match(cells[index])
            }
            if len(found) != 1:
                continue
            side, printed = next(iter(found.items()))
        else:
            placed = _below_row(pages, row, layout)
            if placed is None:
                continue
            side, printed, line = placed
        other = "credit" if side == "debit" else "debit"
        model = {"debit": _number(debit.value), "credit": _number(credit.value)}
        if model[side] is not None and model[side] == _number(printed) and model[other] is not None:
            # Right column, plus a stray amount in the other one that the statement leaves empty.
            row.fields[other] = build_field(
                _SPECS[other], None, evidence=None, page=row.page, confidence=None,
                document=document, low_confidence=low_confidence, evidence_is_row=True,
            )
            row.fields[other].flags.append("column_corrected")
            row.fields[other].messages.append(
                f"The AI also put an amount in the {other} column; the statement leaves it empty, so it was cleared.")
            corrected += 1
            continue
        if model[other] is None or model[other] != _number(printed):
            continue          # already in the right column, or a different amount: leave it to review
        # The printed amount is in the model's wrong column. The statement leaves that column empty, so
        # anything the model put in the right column (e.g. a cheque number) is replaced by the printed cell.
        moved = build_field(
            _SPECS[side], printed, evidence=line.strip(), page=row.page, confidence=None,
            document=document, low_confidence=low_confidence, evidence_is_row=True,
        )
        if moved.status is Status.INVALID:
            continue
        moved.method = "table"
        moved.flags.append("column_corrected")
        moved.messages.append(
            f"The AI put this amount in the {other} column; the statement prints it in the {side} column, "
            "so it was moved there."
        )
        row.fields[side] = moved
        row.fields[other] = build_field(
            _SPECS[other], None, evidence=None, page=row.page, confidence=None,
            document=document, low_confidence=low_confidence, evidence_is_row=True,
        )
        corrected += 1
    return corrected


# ---- Statements laid out in text columns (no ruled table) ----------------------------------------
#
# Some banks (Bank of Baroda) print each transaction on several text lines: the dates, description and
# running balance on one line, the withdrawal or deposit amount on its own line just below it. The column
# an amount is printed in, under the "WITHDRAWAL (DR)" / "DEPOSIT (CR)" / "BALANCE" headings, says what it
# is. These are read from their printed positions and replace what the model put in the wrong field.

_COLUMN_AMOUNT = re.compile(r"(?<![\d,.])\d{1,3}(?:,\d{2,3})*\.\d{2}(?:\s?(?:Dr|Cr)\.?)?(?!\d)", re.IGNORECASE)
_DATED_LINE = re.compile(r"^\s*\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b")
_DEBIT_WORD = re.compile(r"\b(?:withdrawals?|debits?|dr)\b", re.IGNORECASE)
_CREDIT_WORD = re.compile(r"\b(?:deposits?|credits?|cr)\b", re.IGNORECASE)
_BALANCE_WORD = re.compile(r"\bbalance\b", re.IGNORECASE)
_COLUMN_SLACK = 12          # an amount ends at most this many characters from its column heading's end


def _heading_ends(line: str) -> dict[str, int] | None:
    """{"debit"|"credit"|"balance": end position of the heading} from a column-heading line."""
    if _DATED_LINE.match(line) or _SEPARATOR in line:
        return None
    debit = [m.end() for m in _DEBIT_WORD.finditer(line)]
    credit = [m.end() for m in _CREDIT_WORD.finditer(line)]
    balance = [m.end() for m in _BALANCE_WORD.finditer(line)]
    if not debit or not credit or not balance:
        return None
    ends = {"debit": max(debit), "credit": max(credit), "balance": max(balance)}
    return ends if len(set(ends.values())) == 3 else None


def _column_of(end: int, columns: dict[str, int]) -> str | None:
    name, distance = min(((n, abs(end - e)) for n, e in columns.items()), key=lambda t: t[1])
    return name if distance <= _COLUMN_SLACK else None


def _amounts_by_column(lines: list[str], index: int, columns: dict[str, int]) -> dict[str, list[str]]:
    """The amounts printed for the transaction whose date line is ``lines[index]``, by column: on the date
    line itself, and on amount-only lines below it up to the next transaction's date line."""
    found: dict[str, list[str]] = {"debit": [], "credit": [], "balance": []}
    candidates = [lines[index]]
    for line in lines[index + 1:]:
        if _DATED_LINE.match(line):
            break
        if line.strip() and _COLUMN_AMOUNT.fullmatch(line.strip()):
            candidates.append(line)
    for line in candidates:
        for match in _COLUMN_AMOUNT.finditer(line):
            digits_end = match.start() + len(re.match(r"[\d,]+\.\d{2}", match.group()).group())   # before "Dr"/"Cr"
            column = _column_of(digits_end, columns)
            if column is not None:
                found[column].append(match.group().strip())
    return found


def _calibrated(lines: list[str], columns: dict[str, int]) -> dict[str, int]:
    """The heading positions moved to where this page prints its columns. Pages are not always aligned
    alike (a page without headings can sit a few characters further right), but every dated line ends
    with its running balance: the page's usual balance position gives the shift for all columns."""
    ends = []
    for line in lines:
        if _DATED_LINE.match(line):
            balances = [m for m in _COLUMN_AMOUNT.finditer(line) if re.search(r"(?:Dr|Cr)\.?$", m.group(), re.IGNORECASE)]
            if balances:
                last = balances[-1]
                ends.append(last.start() + len(re.match(r"[\d,]+\.\d{2}", last.group()).group()))
    if not ends:
        return columns
    usual = max(set(ends), key=ends.count)
    shift = usual - columns["balance"]
    return {name: end + shift for name, end in columns.items()}


def _dated_lines(rows: list[RowResult], layouts: dict[int, tuple[list[str], dict[str, int]]]) -> list[tuple[RowResult, int]]:
    """Each row with the index of its dated line. A row quoted by its own dated line claims it first. A row
    quoted by a wrapped line (a reference, a name) can belong to the transaction above or below it: it gets
    the one unclaimed dated line within three lines whose printed balance or amount equals what the model
    read for it, and is left alone when none or several fit."""
    located: list[tuple[RowResult, int]] = []
    claimed: set[tuple[int, int]] = set()
    wrapped: list[tuple[RowResult, int]] = []
    for row in rows:
        if not row.row_text or row.page not in layouts or _SEPARATOR in row.row_text:
            continue
        lines, _ = layouts[row.page]
        quoted = " ".join(row.row_text.strip().splitlines()[0].split())
        index = next((i for i, l in enumerate(lines) if " ".join(l.split()) == quoted), None)
        if index is None:
            continue
        if _DATED_LINE.match(lines[index]):
            located.append((row, index))
            claimed.add((row.page, index))
        else:
            wrapped.append((row, index))
    for row, index in wrapped:
        lines, columns = layouts[row.page]
        read = {_number(row.fields[name].value) for name in ("debit", "credit", "balance")
                if row.fields.get(name) is not None and row.fields[name].value is not None}
        fits = []
        for i in range(max(0, index - 3), min(len(lines), index + 4)):
            if (row.page, i) in claimed or not _DATED_LINE.match(lines[i]):
                continue
            printed = _amounts_by_column(lines, i, columns)
            numbers = {_number(re.match(r"[\d,]+\.\d{2}", raw).group()) * (-1 if re.search(r"Dr\.?$", raw, re.I) else 1)
                       if name == "balance" else _number(raw)
                       for name in ("debit", "credit", "balance") for raw in printed[name]}
            if read & numbers:
                fits.append(i)
        if len(fits) == 1:
            located.append((row, fits[0]))
            claimed.add((row.page, fits[0]))
    return located


def _page_layouts(pages: list[PageInput]) -> dict[int, tuple[list[str], dict[str, int]]]:
    """Per page: its lines and where it prints the debit, credit and balance columns."""
    layouts: dict[int, tuple[list[str], dict[str, int]]] = {}
    columns: dict[str, int] | None = None
    for page in sorted(pages, key=lambda p: p.page_number):
        lines = page.text.splitlines()
        for line in lines:
            found = _heading_ends(line)
            if found:
                columns = found
                break
        if columns is not None:               # pages without headings keep the layout of the page before
            layouts[page.page_number] = (lines, _calibrated(lines, columns))
    return layouts


_DATE_TOKEN = re.compile(r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}")
_NOT_A_TRANSACTION = re.compile(r"opening balance|closing balance|brought forward|carried forward|\bB/F\b|\bC/F\b",
                                re.IGNORECASE)


def _layout_rows(layouts: dict[int, tuple[list[str], dict[str, int]]]) -> list[tuple[int, int, dict[str, str | None]]] | None:
    """Every transaction read from its printed columns, in printed order: (page, line index, raw values).
    None when a dated line holds amounts that cannot be told apart (then the layout is not trusted)."""
    out = []
    for page_number in sorted(layouts):
        lines, columns = layouts[page_number]
        for index, line in enumerate(lines):
            if not _DATED_LINE.match(line) or _NOT_A_TRANSACTION.search(line):
                continue
            printed = _amounts_by_column(lines, index, columns)
            amounts = [(side, raw) for side in ("debit", "credit") for raw in printed[side]]
            if not amounts and not printed["balance"]:
                continue                            # a dated line that is not a transaction (a page header)
            if len(amounts) != 1 or len(printed["balance"]) != 1:
                return None
            side, amount = amounts[0]
            dates = _DATE_TOKEN.findall(line)
            first_amount = next(_COLUMN_AMOUNT.finditer(line), None)
            text = line[: first_amount.start()] if first_amount else line
            description = " ".join(_DATE_TOKEN.sub(" ", text).split())
            out.append((page_number, index, {
                "txn_date": dates[0], "value_date": dates[1] if len(dates) > 1 else None,
                "description": description or None, side: amount, "balance": printed["balance"][0],
            }))
    return out


def _chain_holds(amounts: list[tuple[Decimal, Decimal, Decimal]]) -> bool:
    """Each balance = previous balance - debit + credit, in printed order or (newest first) reversed."""
    def holds(seq):
        return all(b[2] == a[2] - b[0] + b[1] for a, b in zip(seq, seq[1:]))
    return len(amounts) >= 2 and (holds(amounts) or holds(list(reversed(amounts))))


def rows_from_layout(result: ExtractionResult, pages: list[PageInput], *, low_confidence: float) -> int:
    """For statements laid out in text columns: read every transaction from its printed columns and, only
    if the running balance confirms every one of them, use these rows instead of the model's (which can
    skip or misplace rows on dense pages). The model's description, reference and cheque number are kept
    where its row has the same date and amount. Returns the number of rows, or 0 when nothing changed."""
    if result.document_type is not DocumentType.BANK_STATEMENT:
        return 0
    layouts = _page_layouts(pages)
    found = _layout_rows(layouts) if layouts else None
    if not found:
        return 0
    document = _DocumentText(pages)
    built: list[RowResult] = []
    chain: list[tuple[Decimal, Decimal, Decimal]] = []
    for page_number, index, values in found:
        line = layouts[page_number][0][index].strip()
        fields = {spec.name: build_field(spec, values.get(spec.name), evidence=line, page=page_number, confidence=None,
                                         document=document, low_confidence=low_confidence, evidence_is_row=True)
                  for spec in TRANSACTION_FIELDS}
        numbers = [fields[n].value for n in ("debit", "credit", "balance")]
        if fields["txn_date"].value is None or numbers[2] is None or (numbers[0] is None) == (numbers[1] is None):
            return 0
        chain.append((Decimal(numbers[0] or 0), Decimal(numbers[1] or 0), Decimal(numbers[2])))
        built.append(RowResult(row_text=line, page=page_number, confidence=1.0, fields=fields))
    if not _chain_holds(chain):
        return 0                                    # not proven: the model's rows (with corrections) stay
    # Keep the model's narration details where its row is the same transaction.
    by_key: dict[tuple, list[RowResult]] = {}
    for row in result.rows:
        amount = next((row.fields[n].value for n in ("debit", "credit")
                       if row.fields.get(n) is not None and row.fields[n].value is not None), None)
        date_value = row.fields["txn_date"].value if row.fields.get("txn_date") else None
        by_key.setdefault((date_value, _number(amount)), []).append(row)
    for row in built:
        amount = row.fields["debit"].value or row.fields["credit"].value
        match = by_key.get((row.fields["txn_date"].value, _number(amount)))
        if match:
            model_row = match.pop(0)
            for name in ("description", "reference_number", "cheque_number"):
                if model_row.fields.get(name) is not None and model_row.fields[name].value is not None:
                    row.fields[name] = model_row.fields[name]
        for name in ("txn_date", "debit", "credit", "balance"):
            row.fields[name].flags.append("from_layout")
    result.rows[:] = built
    result.document_check_issues.append(
        f"Transactions read from the statement's printed columns: the running balance confirms all {len(built)}.")
    return len(built)


def correct_from_layout(result: ExtractionResult, pages: list[PageInput], *, low_confidence: float) -> int:
    """Debit, credit and balance of each transaction read from the column they are printed in, for
    statements laid out in text columns. Only the model's values that differ are replaced; a row whose
    printed amounts are not clear (none, or both a debit and a credit) is left for review. Returns the
    number of rows changed."""
    if result.document_type is not DocumentType.BANK_STATEMENT:
        return 0
    layouts = _page_layouts(pages)
    if not layouts:
        return 0
    document = _DocumentText(pages)
    located = _dated_lines(result.rows, layouts)
    changed = 0
    for row, index in located:
        lines, columns = layouts[row.page]
        printed = _amounts_by_column(lines, index, columns)
        amounts = [(side, raw) for side in ("debit", "credit") for raw in printed[side]]
        updated = False
        if len(amounts) == 1:
            side, raw = amounts[0]
            other = "credit" if side == "debit" else "debit"
            current = {name: _number((row.fields.get(name) or None) and row.fields[name].value) for name in ("debit", "credit")}
            if current[side] != _number(raw) or current[other] is not None:
                field = build_field(_SPECS[side], raw, evidence=lines[index].strip(), page=row.page, confidence=None,
                                    document=document, low_confidence=low_confidence, evidence_is_row=True)
                if field.status is not Status.INVALID:
                    label = "withdrawal" if side == "debit" else "deposit"
                    field.method = "table"
                    field.flags.append("column_from_layout")
                    field.messages.append(f"Read from the statement's {label} column, where this amount is printed.")
                    row.fields[side] = field
                    row.fields[other] = build_field(_SPECS[other], None, evidence=None, page=row.page, confidence=None,
                                                    document=document, low_confidence=low_confidence)
                    updated = True
        if len(printed["balance"]) == 1:
            raw = printed["balance"][0]
            field = build_field(_SPECS["balance"], raw, evidence=lines[index].strip(), page=row.page, confidence=None,
                                document=document, low_confidence=low_confidence, evidence_is_row=True)
            current = row.fields.get("balance")
            if field.status is not Status.INVALID and (current is None or current.value != field.value):
                field.method = "table"
                field.flags.append("column_from_layout")
                field.messages.append("Read from the statement's balance column.")
                row.fields["balance"] = field
                updated = True
        changed += updated
    return changed
