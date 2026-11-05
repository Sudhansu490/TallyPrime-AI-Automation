"""Layout-preserving text keeps table columns distinguishable. SYNTHETIC data."""

from pathlib import Path

import fitz

from tally_automation.processing.layout import Box, render_layout
from tally_automation.processing.pdf_text import extract_pdf_text


def test_prose_keeps_single_spaces() -> None:
    boxes = [Box(0, 0, 30, 10, "Grand"), Box(36, 0, 66, 10, "Total"), Box(72, 0, 120, 10, "1,180.00")]
    assert render_layout(boxes) == "Grand Total 1,180.00"


def test_columns_stay_under_their_headers() -> None:
    w = 6  # points per character
    header = [Box(0, 0, 4 * w, 10, "Date"), Box(40 * w, 0, 50 * w, 10, "Withdrawal"), Box(55 * w, 0, 62 * w, 10, "Deposit")]
    withdrawal = [Box(0, 20, 10 * w, 30, "01-10-2026"), Box(40 * w, 20, 48 * w, 30, "8,378.00")]
    deposit = [Box(0, 40, 10 * w, 50, "07-10-2026"), Box(55 * w, 40, 64 * w, 50, "25,000.00")]
    lines = render_layout(header + withdrawal + deposit).splitlines()
    assert lines[1].index("8,378.00") == lines[0].index("Withdrawal")
    assert lines[2].index("25,000.00") == lines[0].index("Deposit")


def test_rows_are_grouped_by_vertical_position() -> None:
    boxes = [Box(100, 21, 140, 31, "right"), Box(0, 20, 30, 30, "left"), Box(0, 0, 30, 10, "top")]
    assert render_layout(boxes).splitlines()[0] == "top"
    assert render_layout(boxes).splitlines()[1].split() == ["left", "right"]


def test_empty_input() -> None:
    assert render_layout([]) == "" and render_layout([Box(0, 0, 1, 1, "  ")]) == ""


def test_pdf_statement_columns_are_preserved(tmp_path: Path) -> None:
    lines = [
        "Date        Narration           Withdrawal   Deposit     Balance",
        "03-10-2026  SYNTHETIC VENDOR    8,378.00                 91,622.00",
        "07-10-2026  SYNTHETIC CUSTOMER               25,000.00   1,16,622.00",
    ]
    with fitz.open() as pdf:
        page = pdf.new_page()
        for i, line in enumerate(lines):
            page.insert_text((30, 60 + 15 * i), line, fontsize=8, fontname="cour")
        path = tmp_path / "synthetic-statement.pdf"
        pdf.save(path)
    header, debit_row, credit_row = extract_pdf_text(path, min_chars=10)[0].text.splitlines()
    assert credit_row.index("25,000.00") == header.index("Deposit")
    assert debit_row.index("8,378.00") == header.index("Withdrawal")
