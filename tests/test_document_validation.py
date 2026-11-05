from pathlib import Path

import pytest

from synthetic_files import CORRUPT_PDF, make_encrypted_pdf, make_image, make_pdf
from tally_automation.db.models import DocumentType, MediaType
from tally_automation.documents.validation import (
    FileValidationError, detect_media_type, media_type_for_extension, sanitize_filename, validate_content,
)

INVOICE = DocumentType.PURCHASE_INVOICE
STATEMENT = DocumentType.BANK_STATEMENT
LIMITS = {"max_pdf_pages": 5, "max_image_pixels": 1_000_000}


def write(tmp_path: Path, data: bytes, name: str = "upload.bin") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def codes(exc_info: pytest.ExceptionInfo[FileValidationError]) -> str:
    return exc_info.value.code


# ---- File names --------------------------------------------------------------

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("invoice.pdf", "invoice.pdf"),
        ("../../etc/passwd.pdf", "passwd.pdf"),
        ("..\\..\\Windows\\system.pdf", "system.pdf"),
        ("C:\\Users\\x\\Desktop\\bill.PDF", "bill.PDF"),
        ("bad\x00name\x1f.pdf", "badname.pdf"),
        ("  spaced .pdf ", "spaced .pdf"),
    ],
)
def test_sanitize_filename_strips_paths_and_control_chars(raw: str, expected: str) -> None:
    assert sanitize_filename(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "../", "\x00\x01"])
def test_sanitize_filename_rejects_empty(raw: str | None) -> None:
    with pytest.raises(FileValidationError):
        sanitize_filename(raw)


def test_long_filename_keeps_extension() -> None:
    name = sanitize_filename("a" * 400 + ".pdf")
    assert len(name) == 255
    assert name.endswith(".pdf")


# ---- Extensions ----------------------------------------------------------------

@pytest.mark.parametrize(
    ("name", "media"),
    [("a.pdf", MediaType.PDF), ("a.PDF", MediaType.PDF), ("a.jpg", MediaType.JPEG),
     ("a.jpeg", MediaType.JPEG), ("a.png", MediaType.PNG)],
)
def test_invoice_extensions(name: str, media: MediaType) -> None:
    assert media_type_for_extension(name, INVOICE) is media


@pytest.mark.parametrize("name", ["a.exe", "a.pdf.exe", "a.gif", "a.tiff", "a", "a.docx"])
def test_unsupported_extensions_rejected(name: str) -> None:
    with pytest.raises(FileValidationError) as exc:
        media_type_for_extension(name, INVOICE)
    assert codes(exc) == "unsupported_extension"


@pytest.mark.parametrize("name", ["statement.jpg", "statement.png"])
def test_bank_statements_must_be_pdf(name: str) -> None:
    with pytest.raises(FileValidationError) as exc:
        media_type_for_extension(name, STATEMENT)
    assert codes(exc) == "type_not_allowed_for_document"
    assert media_type_for_extension("statement.pdf", STATEMENT) is MediaType.PDF


# ---- Content -------------------------------------------------------------------

def test_signatures_detected() -> None:
    assert detect_media_type(make_pdf()) is MediaType.PDF
    assert detect_media_type(make_image("PNG")) is MediaType.PNG
    assert detect_media_type(make_image("JPEG")) is MediaType.JPEG
    assert detect_media_type(b"GIF89a....") is None
    assert detect_media_type(b"MZ\x90\x00 windows executable") is None


def test_valid_pdf_reports_page_count(tmp_path: Path) -> None:
    result = validate_content(write(tmp_path, make_pdf(pages=3)), MediaType.PDF, **LIMITS)
    assert result.media_type is MediaType.PDF
    assert result.page_count == 3


@pytest.mark.parametrize("fmt,media", [("PNG", MediaType.PNG), ("JPEG", MediaType.JPEG)])
def test_valid_images(tmp_path: Path, fmt: str, media: MediaType) -> None:
    result = validate_content(write(tmp_path, make_image(fmt)), media, **LIMITS)
    assert result.page_count == 1


@pytest.mark.parametrize(
    ("data", "declared", "code"),
    [
        (b"MZ\x90\x00 synthetic executable", MediaType.PDF, "unrecognised_content"),
        (b"<html><script>alert(1)</script></html>", MediaType.PDF, "unrecognised_content"),
        (make_image("PNG"), MediaType.PDF, "extension_mismatch"),
        (make_pdf(), MediaType.PNG, "extension_mismatch"),
        (make_image("JPEG"), MediaType.PNG, "extension_mismatch"),
        (CORRUPT_PDF, MediaType.PDF, "pdf_unreadable"),
        (make_encrypted_pdf(), MediaType.PDF, "pdf_encrypted"),
        (make_pdf(pages=6), MediaType.PDF, "pdf_too_many_pages"),
        (make_image("PNG", (1200, 1000)), MediaType.PNG, "image_too_large"),
        (make_image("JPEG")[:200], MediaType.JPEG, "image_unreadable"),
        (make_image("PNG")[:60], MediaType.PNG, "image_unreadable"),
    ],
    ids=["exe", "html", "png-as-pdf", "pdf-as-png", "jpeg-as-png", "corrupt-pdf", "encrypted-pdf",
         "too-many-pages", "too-many-pixels", "truncated-jpeg", "truncated-png"],
)
def test_bad_content_rejected(tmp_path: Path, data: bytes, declared: MediaType, code: str) -> None:
    with pytest.raises(FileValidationError) as exc:
        validate_content(write(tmp_path, data), declared, **LIMITS)
    assert codes(exc) == code
