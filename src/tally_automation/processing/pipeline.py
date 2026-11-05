"""Text stage of the processing pipeline (steps 5–7): embedded text → OCR → normalise.

Pure functions over a stored file; no database access. The worker persists
the result.

* PDF: embedded text is read for every page. Pages whose text is insufficient
  (see ``pdf_text``) are rendered and OCR'd. If OCR yields more text than the
  embedded layer, OCR becomes the page's source; both raw texts are kept.
* JPEG/PNG: there is no embedded text, so text extraction is ``skipped`` and
  every image is OCR'd.
* No page with any readable text → the document fails; nothing is guessed.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from tally_automation.db.models import MediaType, ProcessingStage, StageStatus, TextSource
from tally_automation.processing.normalize import meaningful_char_count, normalize_text
from tally_automation.processing.ocr import (
    OcrEngine, OcrError, OcrLine, RasterImage, lines_to_text, load_image, render_pdf_page,
)
from tally_automation.processing.pdf_text import extract_pdf_text

logger = logging.getLogger(__name__)

StageCallback = Callable[[ProcessingStage, StageStatus], None]


@dataclass(frozen=True)
class OcrOptions:
    engine: OcrEngine
    min_text_chars: int
    render_dpi: int
    max_pixels: int
    low_confidence: float


@dataclass
class PageResult:
    page_number: int
    text_source: TextSource
    embedded_text: str | None
    ocr_text: str | None
    normalized_text: str
    char_count: int
    ocr_reason: str | None = None
    ocr_engine: str | None = None
    ocr_lines: list[OcrLine] | None = None
    render_dpi: int | None = None
    image_width: int | None = None
    image_height: int | None = None
    low_confidence_lines: int | None = None

    @property
    def mean_confidence(self) -> float | None:
        if not self.ocr_lines:
            return None
        return round(sum(l.confidence for l in self.ocr_lines) / len(self.ocr_lines), 4)

    @property
    def min_confidence(self) -> float | None:
        if not self.ocr_lines:
            return None
        return round(min(l.confidence for l in self.ocr_lines), 4)


@dataclass
class TextOutcome:
    pages: list[PageResult]
    text_extraction: StageStatus
    ocr: StageStatus
    ocr_seconds: float = 0.0
    warnings: list[str] = field(default_factory=list)


class StageError(Exception):
    def __init__(self, stage: ProcessingStage, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


def extract_text(
    path: Path, media_type: MediaType, options: OcrOptions, on_stage: StageCallback | None = None,
) -> TextOutcome:
    notify = on_stage or (lambda stage, status: None)
    if media_type is MediaType.PDF:
        outcome = _extract_pdf(path, options, notify)
    else:
        # Images have no text layer: text extraction does not apply.
        notify(ProcessingStage.TEXT_EXTRACTION, StageStatus.SKIPPED)
        notify(ProcessingStage.OCR, StageStatus.RUNNING)
        started = time.perf_counter()
        page = _ocr_page(1, load_image(path), options, embedded_text=None, reason=None)
        outcome = TextOutcome(
            pages=[page], text_extraction=StageStatus.SKIPPED, ocr=StageStatus.SUCCEEDED,
            ocr_seconds=time.perf_counter() - started,
        )

    empty = [p.page_number for p in outcome.pages if p.char_count == 0]
    if len(empty) == len(outcome.pages):
        last_stage = ProcessingStage.OCR if outcome.ocr is StageStatus.SUCCEEDED else ProcessingStage.TEXT_EXTRACTION
        raise StageError(last_stage, "No readable text was found on any page.")
    if empty:
        outcome.warnings.append(f"No readable text on page(s): {', '.join(map(str, empty))}.")
    return outcome


def _extract_pdf(path: Path, options: OcrOptions, notify: StageCallback) -> TextOutcome:
    try:
        embedded = extract_pdf_text(path, min_chars=options.min_text_chars)
    except Exception as exc:  # PyMuPDF raises its own exception types
        raise StageError(ProcessingStage.TEXT_EXTRACTION, f"PDF text could not be read: {exc}") from exc
    notify(ProcessingStage.TEXT_EXTRACTION, StageStatus.SUCCEEDED)

    to_ocr = [p for p in embedded if p.needs_ocr]
    if not to_ocr:
        notify(ProcessingStage.OCR, StageStatus.SKIPPED)
        pages = [_embedded_page(p.page_number, p.text) for p in embedded]
        return TextOutcome(pages=pages, text_extraction=StageStatus.SUCCEEDED, ocr=StageStatus.SKIPPED)

    notify(ProcessingStage.OCR, StageStatus.RUNNING)
    started = time.perf_counter()
    pages: list[PageResult] = []
    for page in embedded:
        if not page.needs_ocr:
            pages.append(_embedded_page(page.page_number, page.text))
            continue
        image = render_pdf_page(
            path, page.page_number, dpi=options.render_dpi, max_pixels=options.max_pixels,
        )
        pages.append(_ocr_page(page.page_number, image, options, embedded_text=page.text, reason=page.ocr_reason))
    return TextOutcome(
        pages=pages, text_extraction=StageStatus.SUCCEEDED, ocr=StageStatus.SUCCEEDED,
        ocr_seconds=time.perf_counter() - started,
    )


def _embedded_page(page_number: int, raw: str) -> PageResult:
    normalized = normalize_text(raw)
    return PageResult(
        page_number=page_number, text_source=TextSource.EMBEDDED, embedded_text=raw, ocr_text=None,
        normalized_text=normalized, char_count=meaningful_char_count(normalized),
    )


def _ocr_page(
    page_number: int, image: RasterImage, options: OcrOptions,
    *, embedded_text: str | None, reason: str | None,
) -> PageResult:
    try:
        lines = options.engine.recognize(image)
    except OcrError as exc:
        raise StageError(ProcessingStage.OCR, f"Page {page_number}: {exc}") from exc
    ocr_raw = lines_to_text(lines)
    ocr_normalized = normalize_text(ocr_raw)
    ocr_chars = meaningful_char_count(ocr_normalized)

    source, normalized, chars = TextSource.OCR, ocr_normalized, ocr_chars
    if embedded_text is not None:
        embedded_normalized = normalize_text(embedded_text)
        embedded_chars = meaningful_char_count(embedded_normalized)
        # Keep the embedded layer when OCR found no more text than it already had.
        if embedded_chars >= ocr_chars:
            source, normalized, chars = TextSource.EMBEDDED, embedded_normalized, embedded_chars

    return PageResult(
        page_number=page_number, text_source=source, embedded_text=embedded_text, ocr_text=ocr_raw,
        normalized_text=normalized, char_count=chars, ocr_reason=reason,
        ocr_engine=options.engine.name, ocr_lines=lines, render_dpi=image.dpi,
        image_width=image.width, image_height=image.height,
        low_confidence_lines=sum(1 for l in lines if l.confidence < options.low_confidence),
    )
