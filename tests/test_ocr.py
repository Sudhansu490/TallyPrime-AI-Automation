import re
from pathlib import Path

import fitz
import pytest
from PIL import Image

from synthetic_files import SYNTHETIC_INVOICE_LINES, make_pdf, render_text_png
from tally_automation.processing.ocr import (
    OcrLine, PaddleOcrEngine, lines_to_text, load_image, parse_paddle_result, render_pdf_page,
)


def test_parse_paddle_result_converts_boxes_and_handles_empty_pages() -> None:
    box = [[10.2, 5.0], [80.7, 5.0], [80.7, 20.4], [10.2, 20.4]]
    assert parse_paddle_result([[[box, ("Total 1,180.00", 0.97)]]]) == [
        OcrLine("Total 1,180.00", 0.97, (10, 5, 81, 21)),
    ]
    assert parse_paddle_result([None]) == []
    assert parse_paddle_result([[]]) == []
    assert parse_paddle_result(None) == []


def test_lines_to_text_keeps_rows_together_in_reading_order() -> None:
    lines = [
        OcrLine("1,000.00", 0.9, (300, 52, 360, 68)),
        OcrLine("Header", 0.9, (10, 10, 80, 26)),
        OcrLine("Taxable value", 0.9, (10, 50, 120, 66)),
    ]
    header, row = lines_to_text(lines).splitlines()
    assert header == "Header"
    # The amount keeps its column: a wide gap, not merged into the label.
    assert row.startswith("Taxable value") and row.endswith("1,000.00") and "     " in row
    assert lines_to_text([]) == ""


def test_render_pdf_page_produces_bgr_pixels(tmp_path: Path) -> None:
    path = tmp_path / "s.pdf"
    path.write_bytes(make_pdf())
    image = render_pdf_page(path, 1, dpi=100, max_pixels=50_000_000)
    assert image.dpi == 100
    assert image.pixels.shape == (image.height, image.width, 3)
    assert image.width == 827 and abs(image.height - 1169) <= 1  # A4 at 100 DPI


def test_render_pdf_page_lowers_dpi_to_respect_pixel_limit(tmp_path: Path) -> None:
    path = tmp_path / "s.pdf"
    path.write_bytes(make_pdf())
    image = render_pdf_page(path, 1, dpi=600, max_pixels=1_000_000)
    assert image.dpi < 600
    assert image.width * image.height <= 1_000_000


def test_load_image_applies_exif_rotation(tmp_path: Path) -> None:
    image = Image.new("RGB", (40, 20), (255, 255, 255))
    exif = Image.Exif()
    exif[0x0112] = 6  # "rotate 90° clockwise to display"
    path = tmp_path / "s.jpg"
    image.save(path, exif=exif)
    loaded = load_image(path)
    assert (loaded.width, loaded.height) == (20, 40)
    assert loaded.dpi is None


@pytest.mark.ocr
def test_real_paddleocr_reads_synthetic_text(tmp_path: Path) -> None:
    pytest.importorskip("paddleocr")
    path = tmp_path / "s.png"
    path.write_bytes(render_text_png())
    lines = PaddleOcrEngine(det_max_side=2000).recognize(load_image(path))
    text = lines_to_text(lines)
    for expected in ("Invoice No: SYN-0001", "27AAAAA0000A1Z5"):
        assert expected in text, text
    # Known PaddleOCR behaviour on this fixture: "1,180.00" is read as "1.180.00"
    # at 200-300 DPI with ~0.99 confidence. Thousands separators from OCR are
    # therefore NOT trustworthy even at high confidence; the text is stored as
    # recognised and amounts must be validated arithmetically (Phases 4-5).
    assert re.search(r"1[.,]180\.00", text), text
    assert all(0 <= line.confidence <= 1 for line in lines)
    assert min(line.confidence for line in lines) > 0.8
    assert len(lines) == len(SYNTHETIC_INVOICE_LINES)
