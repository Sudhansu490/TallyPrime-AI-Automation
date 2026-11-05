"""Upload validation: extension, signature (magic bytes) and structural checks.

The file's *content* decides its type. The extension must agree with it, but
is never trusted on its own.
"""

from __future__ import annotations

import logging
import unicodedata
import warnings
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

import fitz  # PyMuPDF
from PIL import Image, UnidentifiedImageError

from tally_automation.db.models import DocumentType, MediaType

logger = logging.getLogger(__name__)

EXTENSIONS: dict[str, MediaType] = {
    ".pdf": MediaType.PDF,
    ".jpg": MediaType.JPEG,
    ".jpeg": MediaType.JPEG,
    ".png": MediaType.PNG,
}

# Bank statements are PDF-only (spec §6); invoices may also be scans/photos.
ALLOWED_MEDIA_TYPES: dict[DocumentType, frozenset[MediaType]] = {
    DocumentType.PURCHASE_INVOICE: frozenset({MediaType.PDF, MediaType.JPEG, MediaType.PNG}),
    DocumentType.BANK_STATEMENT: frozenset({MediaType.PDF}),
}

STORAGE_EXTENSION: dict[MediaType, str] = {
    MediaType.PDF: ".pdf",
    MediaType.JPEG: ".jpg",
    MediaType.PNG: ".png",
}

_SIGNATURES: tuple[tuple[bytes, MediaType], ...] = (
    (b"%PDF-", MediaType.PDF),
    (b"\x89PNG\r\n\x1a\n", MediaType.PNG),
    (b"\xff\xd8\xff", MediaType.JPEG),
)
SIGNATURE_BYTES = 1024  # the PDF header may be preceded by junk within the first 1 KiB

_PIL_FORMAT = {"JPEG": MediaType.JPEG, "PNG": MediaType.PNG}
MAX_FILENAME_LENGTH = 255


class FileValidationError(ValueError):
    """The upload is unacceptable. ``code`` is stable; ``message`` is for the user."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ValidatedFile:
    media_type: MediaType
    page_count: int


def allowed_extensions(document_type: DocumentType) -> list[str]:
    allowed = ALLOWED_MEDIA_TYPES[document_type]
    return [ext for ext, media in EXTENSIONS.items() if media in allowed]


def sanitize_filename(raw: str | None) -> str:
    """Return a display-safe base name. Never used to build a filesystem path."""
    name = unicodedata.normalize("NFC", raw or "")
    # Strip any directory part, whichever separator the client used.
    name = PureWindowsPath(PurePosixPath(name).name).name
    name = "".join(ch for ch in name if unicodedata.category(ch)[0] != "C").strip(" .")
    if not name:
        raise FileValidationError("missing_filename", "The file has no name.")
    if len(name) > MAX_FILENAME_LENGTH:
        stem, dot, ext = name.rpartition(".")
        keep = MAX_FILENAME_LENGTH - len(ext) - 1
        name = f"{stem[:keep]}.{ext}" if dot and 0 < len(ext) <= 10 else name[:MAX_FILENAME_LENGTH]
    return name


def media_type_for_extension(filename: str, document_type: DocumentType) -> MediaType:
    extension = Path(filename).suffix.lower()
    media_type = EXTENSIONS.get(extension)
    if media_type is None:
        raise FileValidationError(
            "unsupported_extension",
            f"Unsupported file type {extension or '(none)'}. Allowed: "
            + ", ".join(allowed_extensions(document_type)) + ".",
        )
    if media_type not in ALLOWED_MEDIA_TYPES[document_type]:
        raise FileValidationError(
            "type_not_allowed_for_document",
            f"{extension} files are not accepted for {document_type.value.replace('_', ' ')}s. Allowed: "
            + ", ".join(allowed_extensions(document_type)) + ".",
        )
    return media_type


def detect_media_type(head: bytes) -> MediaType | None:
    for signature, media_type in _SIGNATURES:
        if media_type is MediaType.PDF:
            if signature in head[:SIGNATURE_BYTES]:
                return media_type
        elif head.startswith(signature):
            return media_type
    return None


def validate_content(
    path: Path, declared: MediaType, *, max_pdf_pages: int, max_image_pixels: int
) -> ValidatedFile:
    """Check that the stored bytes really are a readable file of the declared type."""
    with path.open("rb") as handle:
        head = handle.read(SIGNATURE_BYTES)
    detected = detect_media_type(head)
    if detected is None:
        raise FileValidationError(
            "unrecognised_content", "The file content is not a PDF, JPEG or PNG."
        )
    if detected is not declared:
        raise FileValidationError(
            "extension_mismatch",
            f"The file extension says {declared.value} but the content is {detected.value}.",
        )
    if detected is MediaType.PDF:
        return ValidatedFile(detected, _validate_pdf(path, max_pdf_pages))
    _validate_image(path, detected, max_image_pixels)
    return ValidatedFile(detected, 1)


def _validate_pdf(path: Path, max_pages: int) -> int:
    try:
        with fitz.open(path, filetype="pdf") as pdf:
            if pdf.needs_pass:
                raise FileValidationError(
                    "pdf_encrypted",
                    "The PDF is password-protected. Upload an unprotected copy.",
                )
            page_count = pdf.page_count
    except FileValidationError:
        raise
    except Exception as exc:  # PyMuPDF raises several unrelated exception types
        logger.info("pdf rejected as unreadable", extra={"error_type": type(exc).__name__})
        raise FileValidationError("pdf_unreadable", "The PDF is damaged or cannot be read.") from exc
    if page_count < 1:
        raise FileValidationError("pdf_empty", "The PDF has no pages.")
    if page_count > max_pages:
        raise FileValidationError(
            "pdf_too_many_pages", f"The PDF has {page_count} pages; the limit is {max_pages}."
        )
    return page_count


def _validate_image(path: Path, declared: MediaType, max_pixels: int) -> None:
    try:
        with warnings.catch_warnings():
            # Turn Pillow's DecompressionBombWarning into an error.
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                width, height = image.size
                if width * height > max_pixels:
                    raise FileValidationError(
                        "image_too_large",
                        f"The image is {width}×{height} pixels; the limit is "
                        f"{max_pixels // 1_000_000} megapixels.",
                    )
                if _PIL_FORMAT.get(image.format or "") is not declared:
                    raise FileValidationError("extension_mismatch", "The image format does not match its extension.")
                image.verify()
            # verify() leaves the image unusable and misses truncation; do a full decode too.
            with Image.open(path) as image:
                image.load()
    except FileValidationError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError,
            Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        logger.info("image rejected as unreadable", extra={"error_type": type(exc).__name__})
        raise FileValidationError("image_unreadable", "The image is damaged or cannot be read.") from exc
