"""Processing worker and text pipeline, with a fake OCR engine. SYNTHETIC data only."""

from __future__ import annotations

import io
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from synthetic_files import (
    SYNTHETIC_INVOICE_LINES, make_image, make_mixed_pdf, make_pdf, make_scanned_pdf, make_scanner_ocr_pdf,
    render_text_png,
)
from tally_automation.config import Settings
from tally_automation.db.models import (
    Document, DocumentPage, DocumentStatus, DocumentType, JobStatus, ProcessingJob, ProcessingStage,
    StageStatus, TextSource,
)
from tally_automation.db.session import create_session_factory, transaction
from tally_automation.documents.service import DocumentIngestor
from tally_automation.processing.ocr import OcrError, OcrLine, RasterImage
from tally_automation.processing.worker import (
    INTERRUPTED_MESSAGE, ProcessingWorker, RequeueError, requeue_document,
)

INVOICE = DocumentType.PURCHASE_INVOICE


class FakeOcrEngine:
    """Returns fixed SYNTHETIC lines, or nothing, or fails. Records each call."""

    name = "fake-ocr"

    def __init__(self, lines: list[OcrLine] | None = None, error: str | None = None) -> None:
        self.lines = lines if lines is not None else [
            OcrLine(text, 0.95 if i else 0.60, (10, 10 + 30 * i, 300, 30 + 30 * i))
            for i, text in enumerate(SYNTHETIC_INVOICE_LINES)
        ]
        self.error = error
        self.calls: list[RasterImage] = []

    def recognize(self, image: RasterImage) -> list[OcrLine]:
        self.calls.append(image)
        if self.error:
            raise OcrError(self.error)
        return list(self.lines)


@pytest.fixture
def factory(migrated_engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(migrated_engine)


@pytest.fixture
def ingestor(settings: Settings, factory) -> Iterator[DocumentIngestor]:
    settings.ensure_directories()
    yield DocumentIngestor(settings, factory)


def make_worker(settings: Settings, factory, engine: FakeOcrEngine | None = None) -> ProcessingWorker:
    return ProcessingWorker(settings, factory, engine or FakeOcrEngine())


def ingest(ingestor: DocumentIngestor, data: bytes, name: str) -> int:
    return ingestor.ingest(io.BytesIO(data), name, INVOICE).document_id


def load(factory, document_id: int) -> tuple[Document, ProcessingJob, list[DocumentPage]]:
    with factory() as session:
        document = session.get(Document, document_id)
        job = document.jobs[-1]
        pages = list(job.pages)
        return document, job, pages


def test_idle_worker_returns_none(settings, factory) -> None:
    assert make_worker(settings, factory).run_once() is None


def test_text_pdf_uses_embedded_text_without_ocr(settings, factory, ingestor) -> None:
    engine = FakeOcrEngine()
    document_id = ingest(ingestor, make_pdf("SYNTHETIC EMBEDDED INVOICE TEXT", pages=2), "a.pdf")
    worker = make_worker(settings, factory, engine)
    assert worker.run_once() is not None
    assert engine.calls == []

    document, job, pages = load(factory, document_id)
    assert document.status is DocumentStatus.TEXT_EXTRACTED
    assert job.status is JobStatus.SUCCEEDED and job.finished_at is not None
    assert (job.text_extraction_status, job.ocr_status) == (StageStatus.SUCCEEDED, StageStatus.SKIPPED)
    assert (job.ai_extraction_status, job.validation_status) == (StageStatus.PENDING, StageStatus.PENDING)
    assert [p.page_number for p in pages] == [1, 2]
    assert all(p.text_source is TextSource.EMBEDDED and p.ocr_text is None for p in pages)
    assert pages[1].normalized_text == "SYNTHETIC EMBEDDED INVOICE TEXT - page 2"
    assert worker.run_once() is None  # nothing left


def test_scanned_pdf_is_ocrd_with_evidence(settings, factory, ingestor) -> None:
    engine = FakeOcrEngine()
    document_id = ingest(ingestor, make_scanned_pdf(), "scan.pdf")
    make_worker(settings, factory, engine).run_once()

    document, job, [page] = load(factory, document_id)
    assert document.status is DocumentStatus.TEXT_EXTRACTED
    assert (job.text_extraction_status, job.ocr_status) == (StageStatus.SUCCEEDED, StageStatus.SUCCEEDED)
    assert len(engine.calls) == 1 and engine.calls[0].dpi == settings.ocr_render_dpi
    assert page.text_source is TextSource.OCR and page.ocr_engine == "fake-ocr"
    assert page.embedded_text == "" and "0 letters/digits" in page.ocr_reason
    assert page.normalized_text.splitlines() == list(SYNTHETIC_INVOICE_LINES)
    assert page.ocr_lines[0] == {"text": SYNTHETIC_INVOICE_LINES[0], "confidence": 0.6, "bbox": [10, 10, 300, 30]}
    assert page.ocr_low_confidence_lines == 1  # 0.60 < 0.85
    assert (page.ocr_min_confidence, page.ocr_mean_confidence) == (0.6, 0.8625)
    assert (page.image_width, page.image_height) == (engine.calls[0].width, engine.calls[0].height)


def test_image_skips_text_extraction_and_is_ocrd(settings, factory, ingestor) -> None:
    engine = FakeOcrEngine()
    document_id = ingest(ingestor, render_text_png(), "photo.png")
    make_worker(settings, factory, engine).run_once()
    document, job, [page] = load(factory, document_id)
    assert document.status is DocumentStatus.TEXT_EXTRACTED
    assert (job.text_extraction_status, job.ocr_status) == (StageStatus.SKIPPED, StageStatus.SUCCEEDED)
    assert page.text_source is TextSource.OCR and page.embedded_text is None
    assert page.render_dpi is None and page.ocr_reason is None


def test_only_insufficient_pages_are_ocrd(settings, factory, ingestor) -> None:
    engine = FakeOcrEngine(lines=[])  # the blank page has nothing to read
    document_id = ingest(ingestor, make_mixed_pdf(), "mixed.pdf")
    make_worker(settings, factory, engine).run_once()
    document, job, (text_page, blank_page) = load(factory, document_id)
    assert len(engine.calls) == 1
    assert document.status is DocumentStatus.TEXT_EXTRACTED
    assert text_page.text_source is TextSource.EMBEDDED and text_page.ocr_text is None
    assert blank_page.char_count == 0 and blank_page.ocr_text == ""
    # OCR found no more than the (empty) text layer, so the embedded source is kept.
    assert blank_page.text_source is TextSource.EMBEDDED


def test_ocr_text_kept_only_when_it_adds_text(settings, factory, ingestor) -> None:
    engine = FakeOcrEngine(lines=[OcrLine("SYN", 0.99, (0, 0, 10, 10))])
    import fitz
    with fitz.open() as pdf:
        pdf.new_page().insert_text((72, 72), "SYNTHETIC 1234")  # 13 chars < 25 → OCR runs
        data = pdf.tobytes()
    document_id = ingest(ingestor, data, "thin.pdf")
    make_worker(settings, factory, engine).run_once()
    _, _, [page] = load(factory, document_id)
    assert page.text_source is TextSource.EMBEDDED
    assert page.normalized_text == "SYNTHETIC 1234" and page.ocr_text == "SYN"


def test_document_without_any_text_fails(settings, factory, ingestor) -> None:
    document_id = ingest(ingestor, make_image(), "blank.png")
    make_worker(settings, factory, FakeOcrEngine(lines=[])).run_once()
    document, job, pages = load(factory, document_id)
    assert document.status is DocumentStatus.FAILED
    assert job.status is JobStatus.FAILED
    assert job.error_stage is ProcessingStage.OCR and job.ocr_status is StageStatus.FAILED
    assert job.text_extraction_status is StageStatus.SKIPPED  # images have no text layer
    assert job.error_message == "No readable text was found on any page."
    assert pages == []


def test_ocr_engine_error_fails_ocr_stage(settings, factory, ingestor) -> None:
    document_id = ingest(ingestor, make_scanned_pdf(), "scan.pdf")
    make_worker(settings, factory, FakeOcrEngine(error="engine unavailable")).run_once()
    document, job, pages = load(factory, document_id)
    assert document.status is DocumentStatus.FAILED
    assert (job.text_extraction_status, job.ocr_status) == (StageStatus.SUCCEEDED, StageStatus.FAILED)
    assert job.error_stage is ProcessingStage.OCR
    assert job.error_message == "Page 1: engine unavailable"
    assert pages == []


def test_missing_original_fails_text_extraction(settings, factory, ingestor) -> None:
    document_id = ingest(ingestor, make_pdf(), "a.pdf")
    for path in ingestor.storage.originals.rglob("*.pdf"):
        ingestor.storage.remove_original(path.relative_to(ingestor.storage.root).as_posix())
    make_worker(settings, factory).run_once()
    document, job, _ = load(factory, document_id)
    assert document.status is DocumentStatus.FAILED
    assert job.error_stage is ProcessingStage.TEXT_EXTRACTION
    assert job.text_extraction_status is StageStatus.FAILED
    assert job.error_message == "The stored original file is missing."


def test_unexpected_error_is_recorded_not_swallowed(settings, factory, ingestor, monkeypatch) -> None:
    document_id = ingest(ingestor, make_pdf(), "a.pdf")

    def boom(*args, **kwargs):
        raise MemoryError("synthetic failure")

    monkeypatch.setattr("tally_automation.processing.worker.extract_text", boom)
    make_worker(settings, factory).run_once()
    document, job, _ = load(factory, document_id)
    assert document.status is DocumentStatus.FAILED
    assert job.error_message == "Unexpected error: MemoryError: synthetic failure"
    assert job.error_stage is ProcessingStage.TEXT_EXTRACTION


def test_jobs_are_processed_oldest_first_and_duplicates_never(settings, factory, ingestor) -> None:
    data = make_pdf("SYNTHETIC FIRST DOCUMENT TEXT")
    first = ingest(ingestor, data, "1.pdf")
    duplicate = ingest(ingestor, data, "copy.pdf")
    second = ingest(ingestor, make_pdf("SYNTHETIC SECOND DOCUMENT TEXT"), "2.pdf")
    worker = make_worker(settings, factory)
    processed = [worker.run_once(), worker.run_once(), worker.run_once()]
    with factory() as session:
        jobs = {j.id: j.document_id for j in session.scalars(select(ProcessingJob))}
        assert [jobs[j] for j in processed[:2]] == [first, second] and processed[2] is None
        assert session.get(Document, duplicate).status is DocumentStatus.DUPLICATE


def test_interrupted_job_is_marked_failed_on_start(settings, factory, ingestor) -> None:
    document_id = ingest(ingestor, make_scanned_pdf(), "scan.pdf")
    with transaction(factory) as session:
        job = session.scalars(select(ProcessingJob)).one()
        job.status, job.text_extraction_status, job.ocr_status = (
            JobStatus.RUNNING, StageStatus.SUCCEEDED, StageStatus.RUNNING,
        )
        job.document.status = DocumentStatus.PROCESSING
    assert make_worker(settings, factory).recover_interrupted() == 1
    document, job, _ = load(factory, document_id)
    assert document.status is DocumentStatus.FAILED
    assert job.status is JobStatus.FAILED and job.error_message == INTERRUPTED_MESSAGE
    assert (job.text_extraction_status, job.ocr_status) == (StageStatus.SUCCEEDED, StageStatus.FAILED)


def test_requeue_creates_new_attempt_and_keeps_history(settings, factory, ingestor) -> None:
    document_id = ingest(ingestor, make_scanned_pdf(), "scan.pdf")
    make_worker(settings, factory, FakeOcrEngine(error="synthetic outage")).run_once()
    requeue_document(factory, document_id)
    make_worker(settings, factory).run_once()
    with factory() as session:
        document = session.get(Document, document_id)
        assert document.status is DocumentStatus.TEXT_EXTRACTED
        assert [(j.attempt, j.status) for j in document.jobs] == [
            (1, JobStatus.FAILED), (2, JobStatus.SUCCEEDED),
        ]
        assert document.jobs[0].error_message == "Page 1: synthetic outage"


def test_requeue_refuses_documents_that_have_not_failed(settings, factory, ingestor) -> None:
    document_id = ingest(ingestor, make_pdf(), "a.pdf")
    with pytest.raises(RequeueError) as excinfo:
        requeue_document(factory, document_id)
    assert excinfo.value.code == "not_requeueable"
    with pytest.raises(RequeueError) as excinfo:
        requeue_document(factory, 999)
    assert excinfo.value.code == "not_found"


def test_background_thread_processes_and_stops(settings, factory, ingestor) -> None:
    import time
    settings.processing_poll_seconds = 0.05
    document_id = ingest(ingestor, make_pdf(), "a.pdf")
    worker = make_worker(settings, factory)
    worker.start()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with factory() as session:
                if session.get(Document, document_id).status is DocumentStatus.TEXT_EXTRACTED:
                    break
            time.sleep(0.05)
        else:
            pytest.fail("worker did not process the document")
    finally:
        worker.stop()
    assert worker._thread is None


def test_scanner_ocr_layer_is_replaced_by_our_ocr(settings, factory, ingestor) -> None:
    engine = FakeOcrEngine()
    document_id = ingest(ingestor, make_scanner_ocr_pdf(), "searchable-scan.pdf")
    make_worker(settings, factory, engine).run_once()
    _, _, [page] = load(factory, document_id)
    assert len(engine.calls) == 1
    # The scanner's layer has more characters, but ours is used; both are kept as evidence.
    assert page.text_source is TextSource.OCR
    assert page.normalized_text.splitlines() == list(SYNTHETIC_INVOICE_LINES)
    assert "SYN-OOO1" in page.embedded_text and "scanner's own OCR" in page.ocr_reason


def test_scanner_ocr_layer_is_kept_when_our_ocr_reads_too_little(settings, factory, ingestor) -> None:
    engine = FakeOcrEngine(lines=[OcrLine("SYN", 0.99, (0, 0, 10, 10))])
    document_id = ingest(ingestor, make_scanner_ocr_pdf(), "searchable-scan.pdf")
    make_worker(settings, factory, engine).run_once()
    _, _, [page] = load(factory, document_id)
    assert page.text_source is TextSource.EMBEDDED and "SYN-OOO1" in page.normalized_text
