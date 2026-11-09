"""SYNTHETIC test files, generated in memory. They contain no real accounting data."""

from __future__ import annotations

import io

import fitz
from PIL import Image


def make_pdf(text: str = "SYNTHETIC TEST DOCUMENT", pages: int = 1) -> bytes:
    with fitz.open() as pdf:
        for number in range(pages):
            pdf.new_page().insert_text((72, 72), f"{text} - page {number + 1}")
        return pdf.tobytes()


def make_encrypted_pdf() -> bytes:
    with fitz.open() as pdf:
        pdf.new_page().insert_text((72, 72), "SYNTHETIC ENCRYPTED")
        return pdf.tobytes(
            encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="synthetic-owner", user_pw="synthetic-user"
        )


def make_image(fmt: str = "PNG", size: tuple[int, int] = (64, 48)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (200, 220, 240)).save(buffer, format=fmt)
    return buffer.getvalue()


CORRUPT_PDF = b"%PDF-1.7\n%synthetic corrupt body\nthis is not a pdf structure\n"


SYNTHETIC_INVOICE_LINES = (
    "SYNTHETIC TEST INVOICE - NOT A REAL DOCUMENT",
    "Invoice No: SYN-0001",
    "GSTIN: 27AAAAA0000A1Z5",
    "Grand Total 1,180.00",
)


def render_text_png(
    lines: tuple[str, ...] = SYNTHETIC_INVOICE_LINES, dpi: int = 200, a4: bool = False,
) -> bytes:
    """A PNG 'photo' of typed text: no text layer, so only OCR can read it."""
    with fitz.open() as pdf:
        if a4:
            page = pdf.new_page()  # A4 portrait, text at the top like a scanned invoice
        else:
            page = pdf.new_page(width=420, height=40 + 24 * len(lines))
        for index, line in enumerate(lines):
            page.insert_text((24, 32 + index * 24), line, fontsize=12)
        return page.get_pixmap(dpi=dpi).tobytes("png")


def make_scanned_pdf(lines: tuple[str, ...] = SYNTHETIC_INVOICE_LINES) -> bytes:
    """A PDF whose only page content is an image of text (like a scanner's output)."""
    png = render_text_png(lines, a4=True)
    with fitz.open() as pdf:
        page = pdf.new_page()
        page.insert_image(page.rect, stream=png)  # full-page scan, same aspect ratio
        return pdf.tobytes()


SCANNER_OCR_LINES = (  # how a scanner's own OCR misreads the SYNTHETIC invoice
    "SYNTHETlC lNVOlCE SYN-OOO1 dated O1-Se~-2O2#",
    "Taxab|e va|ue 1,OOO.oo IGST 18O.oo Grand tota| 1,18O.oo",
    "Qty 1O.OO NOS Rate 1OO OO NOS HSN 84l5 SYNTHETIC SUPPLIES PVT LTD",
)


def make_scanner_ocr_pdf(*, visible: bool = False) -> bytes:
    """A "searchable" scan: the page image plus the scanner's own OCR as an
    invisible text layer (render mode 3) with plenty of misread text.
    ``visible=True`` draws the same text normally (a designed PDF with a
    full-page background image, whose text is real)."""
    with fitz.open(stream=make_scanned_pdf(), filetype="pdf") as pdf:
        page = pdf[0]
        for index, line in enumerate(SCANNER_OCR_LINES * 3):  # well over 200 letters/digits
            page.insert_text((24, 40 + index * 20), line, fontsize=9, render_mode=0 if visible else 3)
        return pdf.tobytes()


def make_mixed_pdf() -> bytes:
    """Page 1 has a real text layer; page 2 is blank (needs OCR, finds nothing)."""
    with fitz.open() as pdf:
        page = pdf.new_page()
        for index, line in enumerate(SYNTHETIC_INVOICE_LINES):
            page.insert_text((72, 72 + index * 20), line)
        pdf.new_page()
        return pdf.tobytes()


# SYNTHETIC ruled statement table; each narration wraps over two lines in its cell.
RULED_STATEMENT_ROWS = (
    ("Txn No.", "Txn Date", "Description", "Dr Amount", "Cr Amount", "Balance"),
    ("S0000001", "01-04-2026", "NEFT_OUT:SYNB00000001/SYNTHETIC PAYEE ONE/SYNB0000001", "1,000.00", "", "9,000.00 Cr."),
    ("S0000002", "01-04-2026", "NEFT_CHRG:SYNB00000001 SYNTHETIC BANK CHARGES", "5.90", "", "8,994.10 Cr."),
    ("S0000003", "02-04-2026", "NEFT_IN:SYNB00000002/SYNTHETIC CUSTOMER TWO", "", "2,500.00", "11,494.10 Cr."),
)


def make_ruled_statement_pdf() -> bytes:
    """A bank-statement page whose transactions sit in a table drawn with ruling
    lines, the narration wrapping inside a narrow column (like many bank exports)."""
    widths = (70, 70, 150, 70, 70, 90)
    row_height = 40
    x0, y0 = 30, 120
    with fitz.open() as pdf:
        page = pdf.new_page(width=600, height=400)
        page.insert_text((30, 60), "SYNTHETIC BANK ACCOUNT STATEMENT", fontsize=12)
        page.insert_text((30, 80), "Statement Period : 01-04-2026 to 02-04-2026", fontsize=9)
        xs = [x0]
        for w in widths:
            xs.append(xs[-1] + w)
        ys = [y0 + i * row_height for i in range(len(RULED_STATEMENT_ROWS) + 1)]
        for x in xs:
            page.draw_line((x, ys[0]), (x, ys[-1]))
        for y in ys:
            page.draw_line((xs[0], y), (xs[-1], y))
        for r, row in enumerate(RULED_STATEMENT_ROWS):
            for c, cell in enumerate(row):
                rect = fitz.Rect(xs[c] + 2, ys[r] + 2, xs[c + 1] - 2, ys[r + 1] - 2)
                assert page.insert_textbox(rect, cell, fontsize=7) >= 0, cell
        page.insert_text((30, ys[-1] + 30), "Closing Balance 11,494.10 Cr.", fontsize=9)
        return pdf.tobytes()
