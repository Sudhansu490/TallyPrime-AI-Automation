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


def make_mixed_pdf() -> bytes:
    """Page 1 has a real text layer; page 2 is blank (needs OCR, finds nothing)."""
    with fitz.open() as pdf:
        page = pdf.new_page()
        for index, line in enumerate(SYNTHETIC_INVOICE_LINES):
            page.insert_text((72, 72 + index * 20), line)
        pdf.new_page()
        return pdf.tobytes()
