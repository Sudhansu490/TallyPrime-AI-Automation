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
