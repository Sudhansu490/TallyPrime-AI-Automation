"""Document ingestion: pipeline steps 1–4 and job creation.

1. validate the file name and type   2. stream with a size limit
3. SHA-256 while streaming            4. exact-duplicate check (Level 1)

Accepted uploads are stored, recorded and queued for processing. Identical
re-uploads are recorded as ``duplicate`` documents that point at the original
and are not queued. Rejected uploads are not stored at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import BinaryIO

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from tally_automation.config import Settings
from tally_automation.db.models import (
    Document, DocumentFile, DocumentStatus, DocumentType, ProcessingJob,
)
from tally_automation.db.session import transaction
from tally_automation.documents.storage import DocumentStorage
from tally_automation.documents.validation import (
    STORAGE_EXTENSION, ValidatedFile, media_type_for_extension, sanitize_filename, validate_content,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IngestResult:
    document_id: int
    is_duplicate: bool


class DocumentIngestor:
    def __init__(self, settings: Settings, session_factory: sessionmaker[Session]) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.storage = DocumentStorage(settings.upload_dir)

    def ingest(self, source: BinaryIO, filename: str | None, document_type: DocumentType,
               uploaded_by_user_id: int | None = None) -> IngestResult:
        display_name = sanitize_filename(filename)
        declared = media_type_for_extension(display_name, document_type)
        temp_path, sha256, size = self.storage.receive(source, self.settings.max_upload_size_bytes)
        try:
            validated = validate_content(
                temp_path, declared,
                max_pdf_pages=self.settings.max_pdf_pages,
                max_image_pixels=self.settings.max_image_megapixels * 1_000_000,
            )
            relative_path = DocumentStorage.relative_path(sha256, STORAGE_EXTENSION[validated.media_type])
            newly_stored = self.storage.promote(temp_path, relative_path)
        finally:
            self.storage.discard(temp_path)

        args = (sha256, size, relative_path, validated, display_name, document_type, uploaded_by_user_id)
        try:
            try:
                result = self._record(*args)
            except IntegrityError:
                # A concurrent upload of the same bytes committed first. Our transaction
                # was rolled back; recording again finds its row and makes a duplicate.
                result = self._record(*args)
        except Exception:
            if newly_stored and not self._file_recorded(sha256):
                self.storage.remove_original(relative_path)
            raise

        logger.info(
            "document ingested",
            extra={
                "document_id": result.document_id,
                "document_type": document_type.value,
                "duplicate": result.is_duplicate,
                "size_bytes": size,
                "media_type": validated.media_type.value,
            },
        )
        return result

    def _record(
        self, sha256: str, size: int, relative_path: str, validated: ValidatedFile,
        display_name: str, document_type: DocumentType, uploaded_by_user_id: int | None,
    ) -> IngestResult:
        with transaction(self.session_factory) as session:
            stored = session.scalar(select(DocumentFile).where(DocumentFile.sha256 == sha256))
            if stored is not None:
                original = session.scalar(
                    select(Document)
                    .where(Document.file_id == stored.id, Document.duplicate_of_id.is_(None))
                )
                if original is None:  # defensive: a file row always has a primary document
                    raise RuntimeError(f"document_file {stored.id} has no primary document")
                duplicate = Document(
                    file_id=stored.id,
                    document_type=document_type,
                    original_filename=display_name,
                    status=DocumentStatus.DUPLICATE,
                    duplicate_of_id=original.id,
                    uploaded_by_user_id=uploaded_by_user_id,
                )
                session.add(duplicate)
                session.flush()
                return IngestResult(duplicate.id, is_duplicate=True)

            stored = DocumentFile(
                sha256=sha256, size_bytes=size, media_type=validated.media_type,
                storage_path=relative_path, page_count=validated.page_count,
            )
            document = Document(
                file=stored, document_type=document_type, original_filename=display_name,
                status=DocumentStatus.QUEUED, uploaded_by_user_id=uploaded_by_user_id,
            )
            session.add_all([stored, document, ProcessingJob(document=document, attempt=1)])
            session.flush()
            return IngestResult(document.id, is_duplicate=False)

    def _file_recorded(self, sha256: str) -> bool:
        with self.session_factory() as session:
            return session.scalar(
                select(func.count()).select_from(DocumentFile).where(DocumentFile.sha256 == sha256)
            ) > 0
