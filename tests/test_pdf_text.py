from pathlib import Path

from synthetic_files import (
    SYNTHETIC_INVOICE_LINES, make_mixed_pdf, make_pdf, make_scanned_pdf, make_scanner_ocr_pdf,
)
from tally_automation.processing.pdf_text import extract_pdf_text


def write(tmp_path: Path, data: bytes) -> Path:
    path = tmp_path / "synthetic.pdf"
    path.write_bytes(data)
    return path


def test_text_pdf_needs_no_ocr(tmp_path: Path) -> None:
    pages = extract_pdf_text(write(tmp_path, make_pdf("SYNTHETIC INVOICE TEXT LAYER", pages=2)), min_chars=10)
    assert [p.page_number for p in pages] == [1, 2]
    assert all(not p.needs_ocr and p.ocr_reason is None for p in pages)
    assert "SYNTHETIC INVOICE TEXT LAYER - page 2" in pages[1].text


def test_scanned_pdf_needs_ocr(tmp_path: Path) -> None:
    [page] = extract_pdf_text(write(tmp_path, make_scanned_pdf()), min_chars=25)
    assert page.needs_ocr
    assert page.char_count == 0
    assert page.image_coverage > 0.9
    assert "0 letters/digits" in page.ocr_reason


def test_only_insufficient_pages_need_ocr(tmp_path: Path) -> None:
    text_page, blank_page = extract_pdf_text(write(tmp_path, make_mixed_pdf()), min_chars=25)
    assert not text_page.needs_ocr
    assert SYNTHETIC_INVOICE_LINES[1] in text_page.text
    assert blank_page.needs_ocr and blank_page.image_coverage == 0


def test_thin_text_layer_over_full_page_image_needs_ocr(tmp_path: Path) -> None:
    import fitz
    with fitz.open(stream=make_scanned_pdf(), filetype="pdf") as pdf:
        pdf[0].insert_text((20, 20), "SCANNED BY SYNTHETIC SCANNER 2026")  # header over the scan
        data = pdf.tobytes()
    [page] = extract_pdf_text(write(tmp_path, data), min_chars=10)
    assert page.char_count >= 10
    assert page.needs_ocr and "image" in page.ocr_reason


def test_scanner_ocr_text_layer_is_read_again(tmp_path: Path) -> None:
    [page] = extract_pdf_text(write(tmp_path, make_scanner_ocr_pdf()), min_chars=25)
    assert page.char_count >= 200  # plenty of text, but it is the scanner's invisible OCR
    assert page.needs_ocr and page.prefer_ocr
    assert "scanner's own OCR" in page.ocr_reason


def test_visible_text_over_a_background_image_is_trusted(tmp_path: Path) -> None:
    [page] = extract_pdf_text(write(tmp_path, make_scanner_ocr_pdf(visible=True)), min_chars=25)
    assert page.image_coverage > 0.9
    assert not page.needs_ocr and not page.prefer_ocr and page.ocr_reason is None
