"""Uploaded documents, their stored files and their processing jobs.

* ``DocumentFile`` – one row per unique file content (SHA-256). The original
  bytes are stored once, content-addressed, and never modified.
* ``Document`` – one row per upload. A re-upload of identical bytes becomes a
  ``duplicate`` document pointing at the first upload; it is never processed.
* ``ProcessingJob`` – one row per processing attempt for a document.
* ``DocumentPage`` – the text of one page, as produced by one processing job
  (embedded PDF text or OCR), kept raw and normalised, with OCR evidence.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from typing import Any

from sqlalchemy import (
    JSON, CheckConstraint, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from tally_automation.db.base import Base, TimestampMixin, UtcDateTime, str_enum, utc_now


class DocumentType(StrEnum):
    PURCHASE_INVOICE = "purchase_invoice"
    BANK_STATEMENT = "bank_statement"


class MediaType(StrEnum):
    PDF = "application/pdf"
    JPEG = "image/jpeg"
    PNG = "image/png"


class DocumentStatus(StrEnum):
    QUEUED = "queued"            # accepted, waiting for processing
    PROCESSING = "processing"
    # Text extraction / OCR finished. AI extraction (Phase 4) has not run yet.
    TEXT_EXTRACTED = "text_extracted"
    PROCESSED = "processed"      # processing finished; awaiting human review
    FAILED = "failed"
    DUPLICATE = "duplicate"      # identical bytes already uploaded; not processed


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"          # e.g. OCR not needed because the PDF has text


class ProcessingStage(StrEnum):
    TEXT_EXTRACTION = "text_extraction"
    OCR = "ocr"
    AI_EXTRACTION = "ai_extraction"
    VALIDATION = "validation"


class TextSource(StrEnum):
    EMBEDDED = "embedded"        # text layer of the PDF (PyMuPDF)
    OCR = "ocr"                  # recognised from the rendered page or image


class DocumentFile(Base):
    __tablename__ = "document_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    size_bytes: Mapped[int] = mapped_column(Integer)
    media_type: Mapped[MediaType] = mapped_column(str_enum(MediaType, "media_type"))
    # Relative to the upload directory; never derived from the user's filename.
    storage_path: Mapped[str] = mapped_column(String(255), unique=True)
    page_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utc_now)

    documents: Mapped[list[Document]] = relationship(back_populates="file")

    __table_args__ = (
        CheckConstraint("length(sha256) = 64", name="sha256_length"),
        CheckConstraint("size_bytes > 0", name="size_positive"),
        CheckConstraint("page_count > 0", name="page_count_positive"),
    )


class Document(TimestampMixin, Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("document_files.id", ondelete="RESTRICT"))
    document_type: Mapped[DocumentType] = mapped_column(str_enum(DocumentType, "document_type"))
    # Display only (sanitised). Storage never uses it.
    original_filename: Mapped[str] = mapped_column(String(255))
    status: Mapped[DocumentStatus] = mapped_column(str_enum(DocumentStatus, "document_status"))
    duplicate_of_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id", ondelete="RESTRICT")
    )

    file: Mapped[DocumentFile] = relationship(back_populates="documents")
    duplicate_of: Mapped[Document | None] = relationship(remote_side=[id])
    jobs: Mapped[list[ProcessingJob]] = relationship(
        back_populates="document", order_by="ProcessingJob.attempt"
    )

    __table_args__ = (
        # A duplicate always names its original, and only duplicates do.
        CheckConstraint(
            "(status = 'duplicate') = (duplicate_of_id IS NOT NULL)", name="duplicate_has_original"
        ),
        CheckConstraint("duplicate_of_id IS NULL OR duplicate_of_id <> id", name="not_self_duplicate"),
        Index("ix_documents_file_id", "file_id"),
        Index("ix_documents_status", "status"),
        # At most one primary (non-duplicate) document per file. Together with the
        # unique SHA-256 this makes exact-duplicate detection race-safe.
        Index(
            "uq_documents_file_id_primary", "file_id", unique=True,
            sqlite_where=text("duplicate_of_id IS NULL"),
        ),
    )


class ProcessingJob(TimestampMixin, Base):
    __tablename__ = "processing_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="RESTRICT"))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[JobStatus] = mapped_column(str_enum(JobStatus, "job_status"), default=JobStatus.QUEUED)

    text_extraction_status: Mapped[StageStatus] = mapped_column(
        str_enum(StageStatus, "text_extraction_status"), default=StageStatus.PENDING
    )
    ocr_status: Mapped[StageStatus] = mapped_column(
        str_enum(StageStatus, "ocr_status"), default=StageStatus.PENDING
    )
    ai_extraction_status: Mapped[StageStatus] = mapped_column(
        str_enum(StageStatus, "ai_extraction_status"), default=StageStatus.PENDING
    )
    validation_status: Mapped[StageStatus] = mapped_column(
        str_enum(StageStatus, "validation_status"), default=StageStatus.PENDING
    )

    error_stage: Mapped[ProcessingStage | None] = mapped_column(str_enum(ProcessingStage, "processing_stage"))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime())

    document: Mapped[Document] = relationship(back_populates="jobs")
    pages: Mapped[list[DocumentPage]] = relationship(
        back_populates="job", order_by="DocumentPage.page_number"
    )

    __table_args__ = (
        UniqueConstraint("document_id", "attempt"),
        CheckConstraint("attempt > 0", name="attempt_positive"),
        CheckConstraint(
            "(status = 'failed') = (error_message IS NOT NULL)", name="failed_has_error"
        ),
        Index("ix_processing_jobs_status", "status"),
    )


class DocumentPage(Base):
    __tablename__ = "document_pages"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("processing_jobs.id", ondelete="RESTRICT"))
    page_number: Mapped[int] = mapped_column(Integer)
    text_source: Mapped[TextSource] = mapped_column(str_enum(TextSource, "text_source"))
    # Raw text exactly as produced; NULL when that method did not run on this page.
    embedded_text: Mapped[str | None] = mapped_column(Text)
    ocr_text: Mapped[str | None] = mapped_column(Text)
    # Layout-normalised text from ``text_source``. This is what later stages read.
    normalized_text: Mapped[str] = mapped_column(Text)
    char_count: Mapped[int] = mapped_column(Integer)
    # Why OCR was used for a PDF page (NULL for images and for pages not OCR'd).
    ocr_reason: Mapped[str | None] = mapped_column(Text)
    ocr_engine: Mapped[str | None] = mapped_column(String(32))
    # Evidence for later stages: [{"text", "confidence", "bbox": [x0, y0, x1, y1]}]
    # in pixels of the image OCR saw (image_width x image_height, at render_dpi).
    ocr_lines: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    ocr_mean_confidence: Mapped[float | None] = mapped_column(Float)
    ocr_min_confidence: Mapped[float | None] = mapped_column(Float)
    ocr_low_confidence_lines: Mapped[int | None] = mapped_column(Integer)
    render_dpi: Mapped[int | None] = mapped_column(Integer)
    image_width: Mapped[int | None] = mapped_column(Integer)
    image_height: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utc_now)

    job: Mapped[ProcessingJob] = relationship(back_populates="pages")

    __table_args__ = (
        UniqueConstraint("job_id", "page_number"),
        CheckConstraint("page_number > 0", name="page_number_positive"),
        CheckConstraint("char_count >= 0", name="char_count_non_negative"),
        # The chosen source's raw text must exist.
        CheckConstraint(
            "(text_source = 'ocr' AND ocr_text IS NOT NULL AND ocr_engine IS NOT NULL)"
            " OR (text_source = 'embedded' AND embedded_text IS NOT NULL)",
            name="source_text_present",
        ),
    )
