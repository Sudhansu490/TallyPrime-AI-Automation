"""OCR: page/image rasterisation and the PaddleOCR engine (CPU).

The engine is behind a small protocol so tests and later phases can swap it.
PaddleOCR takes ~20 s to import and holds a model in memory, so it is loaded
lazily, once, on first use, and calls are serialised with a lock (the Paddle
predictor is not thread-safe).
"""

from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import fitz
import numpy as np
from PIL import Image, ImageOps

logger = logging.getLogger(__name__)


class OcrError(Exception):
    """OCR could not run (engine unavailable or crashed). Never raised for 'no text found'."""


@dataclass(frozen=True)
class OcrLine:
    text: str
    confidence: float                       # 0..1 as reported by the recogniser
    bbox: tuple[int, int, int, int]         # x0, y0, x1, y1 in image pixels


@dataclass(frozen=True)
class RasterImage:
    pixels: np.ndarray                      # H x W x 3, uint8, BGR (OpenCV order)
    width: int
    height: int
    dpi: int | None                         # known for rendered PDF pages only


class OcrEngine(Protocol):
    name: str

    def recognize(self, image: RasterImage) -> list[OcrLine]: ...


# ---- Rasterisation -----------------------------------------------------------

def render_pdf_page(path: Path, page_number: int, *, dpi: int, max_pixels: int) -> RasterImage:
    """Render one 1-based PDF page to BGR pixels, lowering DPI if it would exceed ``max_pixels``."""
    with fitz.open(path) as pdf:
        page = pdf[page_number - 1]
        width_in, height_in = page.rect.width / 72, page.rect.height / 72
        if width_in * height_in * dpi * dpi > max_pixels:
            dpi = max(72, int(math.sqrt(max_pixels / (width_in * height_in))))
        pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csRGB, alpha=False)
        rgb = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
        return RasterImage(pixels=rgb[:, :, ::-1].copy(), width=pix.width, height=pix.height, dpi=dpi)


def load_image(path: Path) -> RasterImage:
    """Load a JPEG/PNG upright (EXIF orientation applied) as BGR pixels."""
    with Image.open(path) as image:
        upright = ImageOps.exif_transpose(image).convert("RGB")
    rgb = np.asarray(upright, dtype=np.uint8)
    return RasterImage(pixels=rgb[:, :, ::-1].copy(), width=upright.width, height=upright.height, dpi=None)


# ---- PaddleOCR -------------------------------------------------------------------

class PaddleOcrEngine:
    """PaddleOCR 2.x on CPU, English models, with text-angle classification."""

    name = "paddleocr"

    def __init__(self, *, det_max_side: int) -> None:
        self.det_max_side = det_max_side
        self._ocr: Any = None
        self._lock = threading.Lock()

    def _load(self) -> Any:
        if self._ocr is None:
            logger.info("loading OCR engine", extra={"engine": self.name})
            try:
                from paddleocr import PaddleOCR  # heavy import, deferred on purpose
                self._ocr = PaddleOCR(
                    use_angle_cls=True, lang="en", use_gpu=False, show_log=False,
                    det_limit_type="max", det_limit_side_len=self.det_max_side,
                )
            except Exception as exc:  # import errors, missing model files, Paddle init
                raise OcrError(f"OCR engine could not be loaded: {exc}") from exc
            logger.info("OCR engine loaded", extra={"engine": self.name})
        return self._ocr

    def recognize(self, image: RasterImage) -> list[OcrLine]:
        with self._lock:
            ocr = self._load()
            try:
                result = ocr.ocr(image.pixels, cls=True)
            except Exception as exc:
                raise OcrError(f"OCR failed: {exc}") from exc
        return parse_paddle_result(result)


def parse_paddle_result(result: Any) -> list[OcrLine]:
    """Convert PaddleOCR 2.x output ``[[ [box, (text, conf)], ... ]]`` to OcrLines.

    A page with no detected text comes back as ``[None]`` or ``[[]]``.
    """
    lines: list[OcrLine] = []
    for page in result or []:
        for entry in page or []:
            box, (text, confidence) = entry
            xs = [point[0] for point in box]
            ys = [point[1] for point in box]
            lines.append(OcrLine(
                text=str(text),
                confidence=float(confidence),
                bbox=(int(min(xs)), int(min(ys)), int(math.ceil(max(xs))), int(math.ceil(max(ys)))),
            ))
    return lines


def lines_to_text(lines: list[OcrLine], *, row_tolerance: float = 0.5) -> str:
    """Join OCR lines into reading order: rows top-to-bottom, words left-to-right.

    Boxes whose vertical centres are within ``row_tolerance`` × line height of the
    current row's first box are put on the same output line (separated by two
    spaces), which keeps table columns such as "Taxable value   1,000.00" together.
    """
    ordered = sorted(lines, key=lambda l: ((l.bbox[1] + l.bbox[3]) / 2, l.bbox[0]))
    rows: list[list[OcrLine]] = []
    for line in ordered:
        centre = (line.bbox[1] + line.bbox[3]) / 2
        if rows:
            anchor = rows[-1][0]
            anchor_centre = (anchor.bbox[1] + anchor.bbox[3]) / 2
            height = max(anchor.bbox[3] - anchor.bbox[1], 1)
            if abs(centre - anchor_centre) <= height * row_tolerance:
                rows[-1].append(line)
                continue
        rows.append([line])
    return "\n".join(
        "  ".join(l.text for l in sorted(row, key=lambda l: l.bbox[0])) for row in rows
    )
