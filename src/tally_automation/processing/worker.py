"""Background processing worker: claims queued jobs and runs the text stage.

One daemon thread inside the app process handles one job at a time. Job state
lives in the database, so the browser follows progress by polling the API.

Lifecycle of a job: ``queued`` → ``running`` → ``succeeded`` | ``failed``.
On success the document becomes ``text_extracted`` (AI extraction arrives in
Phase 4); on failure it becomes ``failed`` with the stage and reason recorded.

A job still ``running`` at startup was interrupted (crash or shutdown). It is
marked failed, not silently restarted, and can be re-queued from the UI.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload, sessionmaker

from tally_automation.config import Settings
from tally_automation.db.base import utc_now
from tally_automation.db.models import (
    Document, DocumentPage, DocumentStatus, JobStatus, ProcessingJob, ProcessingStage, StageStatus,
)
from tally_automation.db.session import transaction
from tally_automation.documents.storage import DocumentStorage
from tally_automation.processing.ocr import OcrEngine
from tally_automation.processing.pipeline import OcrOptions, PageResult, StageError, extract_text

logger = logging.getLogger(__name__)

STAGE_COLUMN = {
    ProcessingStage.TEXT_EXTRACTION: "text_extraction_status",
    ProcessingStage.OCR: "ocr_status",
    ProcessingStage.AI_EXTRACTION: "ai_extraction_status",
    ProcessingStage.VALIDATION: "validation_status",
}
INTERRUPTED_MESSAGE = "Interrupted: the application stopped while this job was running."


class RequeueError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ProcessingWorker:
    def __init__(
        self, settings: Settings, session_factory: sessionmaker[Session], ocr_engine: OcrEngine,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.storage = DocumentStorage(settings.upload_dir)
        self.options = OcrOptions(
            engine=ocr_engine,
            min_text_chars=settings.ocr_min_text_chars,
            render_dpi=settings.ocr_render_dpi,
            max_pixels=settings.max_image_megapixels * 1_000_000,
            low_confidence=settings.ocr_low_confidence,
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- thread control ----------------------------------------------------------

    def start(self) -> None:
        self.recover_interrupted()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="processing-worker", daemon=True)
        self._thread.start()
        logger.info("processing worker started")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                # Daemon thread mid-OCR; its job is marked interrupted on next start.
                logger.warning("processing worker still busy at shutdown")
            self._thread = None
        logger.info("processing worker stopped")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                job_id = self.run_once()
            except Exception:
                logger.exception("processing worker loop error")
                job_id = None
            if job_id is None:
                self._stop.wait(self.settings.processing_poll_seconds)

    # ---- jobs --------------------------------------------------------------------

    def recover_interrupted(self) -> int:
        with transaction(self.session_factory) as session:
            jobs = session.scalars(
                select(ProcessingJob).options(selectinload(ProcessingJob.document))
                .where(ProcessingJob.status == JobStatus.RUNNING)
            ).all()
            for job in jobs:
                for column in STAGE_COLUMN.values():
                    if getattr(job, column) is StageStatus.RUNNING:
                        setattr(job, column, StageStatus.FAILED)
                job.status = JobStatus.FAILED
                job.error_message = INTERRUPTED_MESSAGE
                job.finished_at = utc_now()
                job.document.status = DocumentStatus.FAILED
        if jobs:
            logger.warning("interrupted jobs marked failed", extra={"count": len(jobs)})
        return len(jobs)

    def run_once(self) -> int | None:
        """Claim and process the oldest queued job. Returns its id, or None if idle."""
        claimed = self._claim()
        if claimed is None:
            return None
        job_id, relative_path, media_type, document_id = claimed
        current = {"stage": ProcessingStage.TEXT_EXTRACTION}

        def on_stage(stage: ProcessingStage, status: StageStatus) -> None:
            if status is StageStatus.RUNNING:
                current["stage"] = stage
            self._set_stage(job_id, stage, status)

        started = time.perf_counter()
        try:
            path: Path = self.storage.resolve(relative_path)
            if not path.is_file():
                raise StageError(ProcessingStage.TEXT_EXTRACTION, "The stored original file is missing.")
            outcome = extract_text(path, media_type, self.options, on_stage)
        except StageError as exc:
            self._fail(job_id, exc.stage, exc.message)
        except Exception as exc:
            logger.exception("unexpected processing error", extra={"job_id": job_id})
            self._fail(job_id, current["stage"], f"Unexpected error: {type(exc).__name__}: {exc}")
        else:
            self._succeed(job_id, outcome.pages, outcome.text_extraction, outcome.ocr)
            ocr_pages = sum(1 for p in outcome.pages if p.ocr_text is not None)
            logger.info(
                "text extracted",
                extra={
                    "job_id": job_id, "document_id": document_id, "pages": len(outcome.pages),
                    "ocr_pages": ocr_pages, "ocr_seconds": round(outcome.ocr_seconds, 1),
                    "seconds": round(time.perf_counter() - started, 1), "warnings": outcome.warnings,
                },
            )
        return job_id

    def _claim(self) -> tuple[int, str, object, int] | None:
        with transaction(self.session_factory) as session:
            job = session.scalar(
                select(ProcessingJob)
                .options(selectinload(ProcessingJob.document).selectinload(Document.file))
                .where(ProcessingJob.status == JobStatus.QUEUED)
                .order_by(ProcessingJob.id).limit(1)
            )
            if job is None:
                return None
            # Conditional update: only one claimer can move a job out of 'queued'.
            claimed = session.execute(
                update(ProcessingJob)
                .where(ProcessingJob.id == job.id, ProcessingJob.status == JobStatus.QUEUED)
                .values(
                    status=JobStatus.RUNNING, started_at=utc_now(), updated_at=utc_now(),
                    text_extraction_status=StageStatus.RUNNING,
                )
                .execution_options(synchronize_session=False)
            ).rowcount
            if claimed != 1:
                return None
            job.document.status = DocumentStatus.PROCESSING
            file = job.document.file
            return job.id, file.storage_path, file.media_type, job.document_id

    def _set_stage(self, job_id: int, stage: ProcessingStage, status: StageStatus) -> None:
        with transaction(self.session_factory) as session:
            session.execute(
                update(ProcessingJob).where(ProcessingJob.id == job_id)
                .values({STAGE_COLUMN[stage]: status, "updated_at": utc_now()})
            )

    def _succeed(
        self, job_id: int, pages: list[PageResult], text_status: StageStatus, ocr_status: StageStatus,
    ) -> None:
        with transaction(self.session_factory) as session:
            job = session.get(ProcessingJob, job_id)
            assert job is not None
            session.add_all(_page_row(job_id, page) for page in pages)
            job.text_extraction_status = text_status
            job.ocr_status = ocr_status
            job.status = JobStatus.SUCCEEDED
            job.finished_at = utc_now()
            job.document.status = DocumentStatus.TEXT_EXTRACTED

    def _fail(self, job_id: int, stage: ProcessingStage, message: str) -> None:
        logger.warning("processing failed", extra={"job_id": job_id, "stage": stage.value, "reason": message})
        with transaction(self.session_factory) as session:
            job = session.get(ProcessingJob, job_id)
            assert job is not None
            setattr(job, STAGE_COLUMN[stage], StageStatus.FAILED)
            # A stage left 'running' by the failure did not finish either.
            for column in STAGE_COLUMN.values():
                if getattr(job, column) is StageStatus.RUNNING:
                    setattr(job, column, StageStatus.FAILED)
            job.status = JobStatus.FAILED
            job.error_stage = stage
            job.error_message = message[:2000]
            job.finished_at = utc_now()
            job.document.status = DocumentStatus.FAILED


def _page_row(job_id: int, page: PageResult) -> DocumentPage:
    return DocumentPage(
        job_id=job_id, page_number=page.page_number, text_source=page.text_source,
        embedded_text=page.embedded_text, ocr_text=page.ocr_text, normalized_text=page.normalized_text,
        char_count=page.char_count, ocr_reason=page.ocr_reason, ocr_engine=page.ocr_engine,
        ocr_lines=None if page.ocr_lines is None else [
            {"text": l.text, "confidence": round(l.confidence, 4), "bbox": list(l.bbox)}
            for l in page.ocr_lines
        ],
        ocr_mean_confidence=page.mean_confidence, ocr_min_confidence=page.min_confidence,
        ocr_low_confidence_lines=page.low_confidence_lines, render_dpi=page.render_dpi,
        image_width=page.image_width, image_height=page.image_height,
    )


def requeue_failed_document(session_factory: sessionmaker[Session], document_id: int) -> int:
    """Queue a new processing attempt for a failed document. Returns the new job id."""
    with transaction(session_factory) as session:
        document = session.scalar(
            select(Document).options(selectinload(Document.jobs)).where(Document.id == document_id)
        )
        if document is None:
            raise RequeueError("not_found", "Document not found.")
        if document.status is not DocumentStatus.FAILED:
            raise RequeueError(
                "not_failed", f"Only failed documents can be reprocessed (this one is {document.status.value}).",
            )
        attempt = max((job.attempt for job in document.jobs), default=0) + 1
        job = ProcessingJob(document=document, attempt=attempt)
        document.status = DocumentStatus.QUEUED
        session.add(job)
        session.flush()
        logger.info("document re-queued", extra={"document_id": document_id, "attempt": attempt})
        return job.id
