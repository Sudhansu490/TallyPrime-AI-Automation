"""Debit/credit amounts the LLM skipped, read from a ruled statement table.

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


def fill_skipped_amounts(
    result: ExtractionResult, pages: list[PageInput], *, low_confidence: float,
) -> int:
    """Fill debit or credit on statement rows where the model returned neither.
    Returns the number of rows filled."""
    if result.document_type is not DocumentType.BANK_STATEMENT:
        return 0
    columns = _amount_columns(pages)
    if columns is None:
        return 0
    document = _DocumentText(pages)
    filled = 0
    for row in result.rows:
        debit, credit = row.fields.get("debit"), row.fields.get("credit")
        if debit is None or credit is None or debit.raw is not None or credit.raw is not None:
            continue
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
        label = "debit" if name == "debit" else "credit"
        field = build_field(
            _SPECS[name], raw, evidence=line.strip(), page=row.page, confidence=None,
            document=document, low_confidence=low_confidence, evidence_is_row=True,
        )
        if field.status is Status.INVALID:
            continue  # never keep a value the usual checks reject
        field.method = "table"
        field.flags.append("read_from_table")
        field.messages.append(
            f"The AI returned no amount for this row; this {label} was copied from the statement's "
            f"{label} column. Check it against the statement."
        )
        field.status = Status.WARNING
        row.fields[name] = field
        filled += 1
    return filled

