"""Ingestion service and the database constraints behind exact-duplicate detection."""

import io
import threading
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from synthetic_files import make_image, make_pdf
from tally_automation.config import Settings
from tally_automation.db.models import (
    Document, DocumentFile, DocumentStatus, DocumentType, JobStatus, ProcessingJob, StageStatus,
)
from tally_automation.db.session import create_session_factory, transaction
from tally_automation.documents.service import DocumentIngestor
from tally_automation.documents.validation import FileValidationError

INVOICE = DocumentType.PURCHASE_INVOICE


@pytest.fixture
def factory(migrated_engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(migrated_engine)


@pytest.fixture
def ingestor(settings: Settings, factory: sessionmaker[Session]) -> Iterator[DocumentIngestor]:
    settings.ensure_directories()
    yield DocumentIngestor(settings, factory)


def stored_files(ingestor: DocumentIngestor) -> list:
    return [p for p in ingestor.storage.originals.rglob("*") if p.is_file()]


def test_new_document_is_stored_and_queued(ingestor: DocumentIngestor, factory) -> None:
    data = make_pdf(pages=2)
    result = ingestor.ingest(io.BytesIO(data), "invoice.pdf", INVOICE)
    assert result.is_duplicate is False
    with factory() as session:
        document = session.get(Document, result.document_id)
        assert document.status is DocumentStatus.QUEUED
        assert document.original_filename == "invoice.pdf"
        assert document.file.page_count == 2
        [job] = document.jobs
        assert job.status is JobStatus.QUEUED and job.attempt == 1
        assert job.ocr_status is StageStatus.PENDING
    [path] = stored_files(ingestor)
    assert path.read_bytes() == data
    assert "invoice" not in path.name  # the user's name never reaches the filesystem


@pytest.mark.parametrize("second_type", [INVOICE, DocumentType.BANK_STATEMENT])
def test_identical_bytes_become_duplicate(ingestor: DocumentIngestor, factory, second_type) -> None:
    data = make_pdf()
    first = ingestor.ingest(io.BytesIO(data), "a.pdf", INVOICE)
    second = ingestor.ingest(io.BytesIO(data), "renamed copy.pdf", second_type)
    assert second.is_duplicate is True
    with factory() as session:
        duplicate = session.get(Document, second.document_id)
        assert duplicate.status is DocumentStatus.DUPLICATE
        assert duplicate.duplicate_of_id == first.document_id
        assert duplicate.jobs == []  # never processed
        assert session.scalar(select(func.count()).select_from(DocumentFile)) == 1
    assert len(stored_files(ingestor)) == 1


def test_different_content_is_not_duplicate(ingestor: DocumentIngestor) -> None:
    first = ingestor.ingest(io.BytesIO(make_pdf("SYNTHETIC A")), "a.pdf", INVOICE)
    second = ingestor.ingest(io.BytesIO(make_pdf("SYNTHETIC B")), "a.pdf", INVOICE)
    assert not first.is_duplicate and not second.is_duplicate


def test_rejected_upload_leaves_nothing_behind(ingestor: DocumentIngestor, factory) -> None:
    with pytest.raises(FileValidationError):
        ingestor.ingest(io.BytesIO(make_image("PNG")), "statement.pdf", INVOICE)
    with pytest.raises(FileValidationError):
        ingestor.ingest(io.BytesIO(make_image("PNG")), "statement.png", DocumentType.BANK_STATEMENT)
    assert stored_files(ingestor) == []
    assert list(ingestor.storage.incoming.iterdir()) == []
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Document)) == 0


def test_database_failure_removes_newly_stored_file(ingestor: DocumentIngestor, monkeypatch) -> None:
    def broken(*_args: object) -> None:
        raise RuntimeError("synthetic database failure")

    monkeypatch.setattr(ingestor, "_record", broken)
    with pytest.raises(RuntimeError):
        ingestor.ingest(io.BytesIO(make_pdf()), "a.pdf", INVOICE)
    assert stored_files(ingestor) == []


def test_concurrent_identical_uploads_yield_one_original(ingestor: DocumentIngestor, factory) -> None:
    data = make_pdf("SYNTHETIC CONCURRENT")
    barrier = threading.Barrier(4)
    results, errors = [], []

    def upload() -> None:
        barrier.wait()
        try:
            results.append(ingestor.ingest(io.BytesIO(data), "same.pdf", INVOICE))
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=upload) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert sorted(r.is_duplicate for r in results) == [False, True, True, True]
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ProcessingJob)) == 1


# ---- Constraints enforced by the database itself --------------------------------

def _file(session: Session, sha: str = "a" * 64) -> DocumentFile:
    file = DocumentFile(sha256=sha, size_bytes=1, media_type="application/pdf",
                        storage_path=f"originals/aa/{sha}.pdf", page_count=1)
    session.add(file)
    session.flush()
    return file


def test_two_primary_documents_for_one_file_rejected(factory) -> None:
    with pytest.raises(IntegrityError):
        with transaction(factory) as session:
            file = _file(session)
            for _ in range(2):
                session.add(Document(file=file, document_type=INVOICE, original_filename="x.pdf",
                                     status=DocumentStatus.QUEUED))
            session.flush()


def test_duplicate_sha256_rejected(factory) -> None:
    with pytest.raises(IntegrityError):
        with transaction(factory) as session:
            _file(session)
            session.add(DocumentFile(sha256="a" * 64, size_bytes=1, media_type="application/pdf",
                                     storage_path="originals/aa/other.pdf", page_count=1))
            session.flush()


@pytest.mark.parametrize(
    "sql",
    [
        # duplicate status without an original
        "INSERT INTO documents (file_id, document_type, original_filename, status) "
        "VALUES (1, 'purchase_invoice', 'x', 'duplicate')",
        # unknown document type
        "INSERT INTO documents (file_id, document_type, original_filename, status) "
        "VALUES (1, 'sales_invoice', 'x', 'queued')",
        # failed job without an error message
        "INSERT INTO processing_jobs (document_id, attempt, status, text_extraction_status, ocr_status, "
        "ai_extraction_status, validation_status) VALUES (1, 2, 'failed', 'pending', 'pending', 'pending', 'pending')",
        # missing parent file
        "INSERT INTO documents (file_id, document_type, original_filename, status) "
        "VALUES (999, 'purchase_invoice', 'x', 'queued')",
    ],
    ids=["duplicate-without-original", "bad-type", "failed-without-error", "missing-file-fk"],
)
def test_invalid_rows_rejected_by_database(ingestor: DocumentIngestor, migrated_engine: Engine, sql: str) -> None:
    ingestor.ingest(io.BytesIO(make_pdf()), "a.pdf", INVOICE)  # creates file 1 / document 1
    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as connection:
            connection.execute(text(sql))
