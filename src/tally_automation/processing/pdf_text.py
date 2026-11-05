"""Embedded-text extraction from PDFs with PyMuPDF, and the per-page OCR decision.

OCR is slow and less exact than embedded text, so a page is OCR'd only when
its embedded text is insufficient:

* fewer than ``min_chars`` letters/digits (scanned page, or a page whose
  text layer is empty), or
* most of the page is covered by images and the text layer is still thin
  (a scan with a small typed header or stamp on top).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import fitz

from tally_automation.processing.normalize import meaningful_char_count

# A page mostly covered by images with less text than this is treated as a scan.
IMAGE_COVERAGE_THRESHOLD = 0.5
SCANNED_PAGE_TEXT_CEILING = 200


@dataclass(frozen=True)
class PdfPageText:
    page_number: int          # 1-based
    text: str                 # raw embedded text, reading order
    char_count: int           # letters and digits only
    image_coverage: float     # 0..1 share of the page area covered by images
    needs_ocr: bool
    ocr_reason: str | None


def extract_pdf_text(path: Path, *, min_chars: int) -> list[PdfPageText]:
    pages: list[PdfPageText] = []
    with fitz.open(path) as pdf:
        for index, page in enumerate(pdf):
            # sort=True orders blocks top-to-bottom, left-to-right (reading order).
            text = page.get_text("text", sort=True)
            chars = meaningful_char_count(text)
            coverage = _image_coverage(page)
            reason: str | None = None
            if chars < min_chars:
                reason = f"embedded text has {chars} letters/digits (minimum {min_chars})"
            elif coverage >= IMAGE_COVERAGE_THRESHOLD and chars < SCANNED_PAGE_TEXT_CEILING:
                reason = (
                    f"page is {coverage:.0%} image with only {chars} letters/digits of embedded text"
                )
            pages.append(PdfPageText(
                page_number=index + 1, text=text, char_count=chars,
                image_coverage=round(coverage, 3), needs_ocr=reason is not None, ocr_reason=reason,
            ))
    return pages


def _image_coverage(page: fitz.Page) -> float:
    page_rect = page.rect
    page_area = page_rect.width * page_rect.height
    if page_area <= 0:
        return 0.0
    covered = 0.0
    for info in page.get_image_info():
        box = fitz.Rect(info["bbox"]) & page_rect
        if not box.is_empty:
            covered += box.width * box.height
    # Overlapping images can sum past the page area.
    return min(covered / page_area, 1.0)
