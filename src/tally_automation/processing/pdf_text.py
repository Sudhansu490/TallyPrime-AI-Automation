"""Embedded-text extraction from PDFs with PyMuPDF, and the per-page OCR decision.

OCR is slow and less exact than embedded text, so a page is OCR'd only when
its embedded text is insufficient:

* fewer than ``min_chars`` letters/digits (scanned page, or a page whose
  text layer is empty), or
* most of the page is covered by images and the text layer is still thin
  (a scan with a small typed header or stamp on top), or
* most of the page is covered by images and most of its text is invisible:
  a "searchable" scan whose text layer was added by the scanner's own OCR
  (e.g. a scanner utility's PDF output). That text is often wrong (``0`` read
  as ``o``, garbled dates), so the page is read again with this app's OCR and
  the result is preferred over the scanner's layer (``prefer_ocr``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import fitz

from tally_automation.processing.layout import Box, render_layout
from tally_automation.processing.normalize import meaningful_char_count

# A page mostly covered by images with less text than this is treated as a scan.
IMAGE_COVERAGE_THRESHOLD = 0.5
SCANNED_PAGE_TEXT_CEILING = 200
# Share of a page's text characters drawn invisibly (PDF text render mode 3)
# above which the text layer is taken to be another program's OCR.
INVISIBLE_TEXT_SHARE = 0.5
_INVISIBLE_RENDER_MODE = 3


@dataclass(frozen=True)
class PdfPageText:
    page_number: int          # 1-based
    text: str                 # raw embedded text, reading order, columns kept aligned
    char_count: int           # letters and digits only
    image_coverage: float     # 0..1 share of the page area covered by images
    needs_ocr: bool
    ocr_reason: str | None
    # True when the text layer is a scanner's invisible OCR: this app's OCR result
    # is used instead of it whenever OCR reads enough text.
    prefer_ocr: bool = False


def extract_pdf_text(path: Path, *, min_chars: int) -> list[PdfPageText]:
    pages: list[PdfPageText] = []
    with fitz.open(path) as pdf:
        for index, page in enumerate(pdf):
            # Words with coordinates, laid out on a grid so table columns stay
            # distinguishable (e.g. Withdrawal vs Deposit on a bank statement).
            words = page.get_text("words", sort=True)
            text = render_layout([Box(w[0], w[1], w[2], w[3], w[4]) for w in words])
            chars = meaningful_char_count(text)
            coverage = _image_coverage(page)
            reason: str | None = None
            # A scan carrying another program's invisible OCR layer: our OCR wins.
            prefer_ocr = (
                chars > 0 and coverage >= IMAGE_COVERAGE_THRESHOLD
                and _invisible_share(page) >= INVISIBLE_TEXT_SHARE
            )
            if chars < min_chars:
                reason = f"embedded text has {chars} letters/digits (minimum {min_chars})"
            elif prefer_ocr:
                reason = (
                    f"page is a {coverage:.0%} image scan whose text layer is invisible text added by "
                    "the scanner's own OCR, which is not reliable"
                )
            elif coverage >= IMAGE_COVERAGE_THRESHOLD and chars < SCANNED_PAGE_TEXT_CEILING:
                reason = (
                    f"page is {coverage:.0%} image with only {chars} letters/digits of embedded text"
                )
            pages.append(PdfPageText(
                page_number=index + 1, text=text, char_count=chars,
                image_coverage=round(coverage, 3), needs_ocr=reason is not None, ocr_reason=reason,
                prefer_ocr=prefer_ocr,
            ))
    return pages


def _invisible_share(page: fitz.Page) -> float:
    """Share of the page's text characters drawn invisibly (render mode 3)."""
    total = invisible = 0
    for span in page.get_texttrace():
        count = len(span["chars"])
        total += count
        if span["type"] == _INVISIBLE_RENDER_MODE:
            invisible += count
    return invisible / total if total else 0.0


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
