"""Document upload, listing, status, extracted text and reprocessing endpoints."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload
from starlette.datastructures import UploadFile

from tally_automation.api.deps import IngestorDep, SessionDep, SettingsDep
from tally_automation.db.models import (
    Document, DocumentPage, DocumentStatus, DocumentType, JobStatus, MediaType, ProcessingJob,
    ProcessingStage, StageStatus, TextSource,
)
from tally_automation.documents.validation import FileValidationError, allowed_extensions
from tally_automation.processing.worker import RequeueError, requeue_document

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/documents", tags=["documents"])

# Room for multipart boundaries and the small document_type field.
MULTIPART_OVERHEAD_BYTES = 64 * 1024


# ---- Schemas -----------------------------------------------------------------

class StageStatuses(BaseModel):
    text_extraction: StageStatus
    ocr: StageStatus
    ai_extraction: StageStatus
    validation: StageStatus


class JobOut(BaseModel):
    id: int
    attempt: int
    status: JobStatus
    stages: StageStatuses
    error_stage: ProcessingStage | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class FileOut(BaseModel):
    sha256: str
    size_bytes: int
    media_type: MediaType
    page_count: int


class DuplicateOf(BaseModel):
    id: int
    original_filename: str
    document_type: DocumentType
    uploaded_at: datetime


class DuplicateEvidence(BaseModel):
    level: Literal["exact_file"]
    description: str
    duplicate_of: DuplicateOf


class DocumentOut(BaseModel):
    id: int
    document_type: DocumentType
    original_filename: str
    status: DocumentStatus
    uploaded_at: datetime
    file: FileOut
    duplicate: DuplicateEvidence | None
    latest_job: JobOut | None


class DocumentList(BaseModel):
    items: list[DocumentOut]
    total: int
    limit: int
    offset: int


class DocumentStats(BaseModel):
    total: int
    queued: int
    processing: int
    text_extracted: int
    processed: int
    failed: int
    duplicates: int
    # Vouchers and approvals do not exist before Phase 8; null means "not available".
    pending_approvals: int | None


class UploadLimits(BaseModel):
    max_size_bytes: int
    max_pdf_pages: int
    allowed_extensions: dict[DocumentType, list[str]]


class OcrLineOut(BaseModel):
    text: str
    confidence: float
    bbox: list[int]
    low_confidence: bool


class PageTextOut(BaseModel):
    page_number: int
    text_source: TextSource
    text: str                       # normalised text from text_source
    char_count: int
    embedded_text: str | None       # raw PDF text layer, when it was read
    ocr_text: str | None            # raw OCR output, when OCR ran
    ocr_reason: str | None
    ocr_engine: str | None
    ocr_mean_confidence: float | None
    ocr_min_confidence: float | None
    ocr_low_confidence_lines: int | None
    ocr_lines: list[OcrLineOut] | None
    render_dpi: int | None
    image_width: int | None
    image_height: int | None


class DocumentTextOut(BaseModel):
    document_id: int
    job_id: int
    attempt: int
    low_confidence_threshold: float
    pages: list[PageTextOut]


class ErrorOut(BaseModel):
    detail: str
    code: str | None = None


# ---- Serialisation ---------------------------------------------------------

def _job_out(job: ProcessingJob) -> JobOut:
    return JobOut(
        id=job.id, attempt=job.attempt, status=job.status,
        stages=StageStatuses(
            text_extraction=job.text_extraction_status, ocr=job.ocr_status,
            ai_extraction=job.ai_extraction_status, validation=job.validation_status,
        ),
        error_stage=job.error_stage, error_message=job.error_message,
        created_at=job.created_at, started_at=job.started_at, finished_at=job.finished_at,
    )


def _document_out(document: Document) -> DocumentOut:
    duplicate = None
    if document.duplicate_of is not None:
        original = document.duplicate_of
        duplicate = DuplicateEvidence(
            level="exact_file",
            description=(
                "Byte-for-byte identical file (same SHA-256) to document "
                f"#{original.id}. It was not processed again."
            ),
            duplicate_of=DuplicateOf(
                id=original.id, original_filename=original.original_filename,
                document_type=original.document_type, uploaded_at=original.created_at,
            ),
        )
    return DocumentOut(
        id=document.id, document_type=document.document_type,
        original_filename=document.original_filename, status=document.status,
        uploaded_at=document.created_at,
        file=FileOut(
            sha256=document.file.sha256, size_bytes=document.file.size_bytes,
            media_type=document.file.media_type, page_count=document.file.page_count,
        ),
        duplicate=duplicate,
        latest_job=_job_out(document.jobs[-1]) if document.jobs else None,
    )


def _document_query():
    return select(Document).options(
        selectinload(Document.file), selectinload(Document.jobs), selectinload(Document.duplicate_of),
    )


def _load_document(session: Session, document_id: int) -> DocumentOut | None:
    document = session.scalar(_document_query().where(Document.id == document_id))
    return _document_out(document) if document else None


def _error(status_code: int, detail: str, code: str | None = None) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail, "code": code})


# ---- Endpoints -------------------------------------------------------------

@router.get("/upload-limits", response_model=UploadLimits)
def upload_limits(settings: SettingsDep) -> UploadLimits:
    return UploadLimits(
        max_size_bytes=settings.max_upload_size_bytes,
        max_pdf_pages=settings.max_pdf_pages,
        allowed_extensions={t: allowed_extensions(t) for t in DocumentType},
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=DocumentOut,
    responses={400: {"model": ErrorOut}, 411: {"model": ErrorOut}, 413: {"model": ErrorOut},
               422: {"model": ErrorOut}},
    openapi_extra={"requestBody": {"content": {"multipart/form-data": {"schema": {
        "type": "object", "required": ["file", "document_type"],
        "properties": {
            "file": {"type": "string", "format": "binary"},
            "document_type": {"type": "string", "enum": [t.value for t in DocumentType]},
        },
    }}}}},
)
async def upload_document(
    request: Request, settings: SettingsDep, ingestor: IngestorDep
) -> DocumentOut | JSONResponse:
    """Upload one file. Identical re-uploads are recorded as duplicates (still 201)."""
    # Enforce the size limit before reading the body. The HTTP server frames the
    # body by Content-Length, so a client cannot send more than it declared.
    content_length = request.headers.get("content-length")
    if content_length is None:
        return _error(status.HTTP_411_LENGTH_REQUIRED, "Content-Length is required.", "length_required")
    try:
        declared_length = int(content_length)
    except ValueError:
        return _error(status.HTTP_400_BAD_REQUEST, "Invalid Content-Length.", "bad_content_length")
    if declared_length > settings.max_upload_size_bytes + MULTIPART_OVERHEAD_BYTES:
        return _error(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"The file exceeds the {settings.max_upload_size_mb} MB limit.", "file_too_large",
        )

    content_type = request.headers.get("content-type", "")
    if not content_type.startswith("multipart/form-data"):
        return _error(status.HTTP_400_BAD_REQUEST, "Expected multipart/form-data.", "not_multipart")

    form = await request.form(max_files=1, max_fields=1, max_part_size=1024)
    try:
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            return _error(422, "No file was uploaded.", "missing_file")
        try:
            document_type = DocumentType(str(form.get("document_type") or ""))
        except ValueError:
            return _error(
                422, "document_type must be one of: " + ", ".join(t.value for t in DocumentType) + ".",
                "invalid_document_type",
            )
        try:
            result = await run_in_threadpool(ingestor.ingest, upload.file, upload.filename, document_type)
        except FileValidationError as exc:
            logger.info("upload rejected", extra={"code": exc.code, "document_type": document_type.value})
            code = 413 if exc.code == "file_too_large" else 422
            return _error(code, exc.message, exc.code)
    finally:
        await form.close()

    def load() -> DocumentOut | None:
        with ingestor.session_factory() as session:
            return _load_document(session, result.document_id)

    document = await run_in_threadpool(load)
    if document is None:  # pragma: no cover - committed moments ago
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Document not found after upload.")
    return document


@router.get("", response_model=DocumentList)
def list_documents(
    session: SessionDep,
    status_filter: Annotated[DocumentStatus | None, Query(alias="status")] = None,
    document_type: DocumentType | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DocumentList:
    conditions = []
    if status_filter is not None:
        conditions.append(Document.status == status_filter)
    if document_type is not None:
        conditions.append(Document.document_type == document_type)
    total = session.scalar(select(func.count()).select_from(Document).where(*conditions)) or 0
    documents = session.scalars(
        _document_query().where(*conditions).order_by(Document.id.desc()).limit(limit).offset(offset)
    ).all()
    return DocumentList(items=[_document_out(d) for d in documents], total=total, limit=limit, offset=offset)


@router.get("/stats", response_model=DocumentStats)
def document_stats(session: SessionDep) -> DocumentStats:
    counts = dict(session.execute(select(Document.status, func.count()).group_by(Document.status)).all())
    return DocumentStats(
        total=sum(counts.values()),
        queued=counts.get(DocumentStatus.QUEUED, 0),
        processing=counts.get(DocumentStatus.PROCESSING, 0),
        text_extracted=counts.get(DocumentStatus.TEXT_EXTRACTED, 0),
        processed=counts.get(DocumentStatus.PROCESSED, 0),
        failed=counts.get(DocumentStatus.FAILED, 0),
        duplicates=counts.get(DocumentStatus.DUPLICATE, 0),
        pending_approvals=None,
    )


@router.get("/{document_id}", response_model=DocumentOut, responses={404: {"model": ErrorOut}})
def get_document(document_id: int, session: SessionDep) -> DocumentOut:
    document = _load_document(session, document_id)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found.")
    return document


@router.get(
    "/{document_id}/text", response_model=DocumentTextOut, responses={404: {"model": ErrorOut}},
)
def get_document_text(document_id: int, session: SessionDep, settings: SettingsDep) -> DocumentTextOut:
    """Page text of the latest attempt that produced text, page by page.

    That attempt may have failed at a later stage (e.g. AI extraction); its
    text is still shown, because it helps explain the failure.
    """
    job = session.scalar(
        select(ProcessingJob)
        .options(selectinload(ProcessingJob.pages))
        .where(ProcessingJob.document_id == document_id, ProcessingJob.pages.any())
        .order_by(ProcessingJob.attempt.desc()).limit(1)
    )
    if job is None:
        if session.get(Document, document_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found.")
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No extracted text yet for this document.")
    threshold = settings.ocr_low_confidence
    return DocumentTextOut(
        document_id=document_id, job_id=job.id, attempt=job.attempt,
        low_confidence_threshold=threshold, pages=[_page_out(page, threshold) for page in job.pages],
    )


def _page_out(page: DocumentPage, threshold: float) -> PageTextOut:
    lines = None
    if page.ocr_lines is not None:
        lines = [
            OcrLineOut(
                text=line["text"], confidence=line["confidence"], bbox=line["bbox"],
                low_confidence=line["confidence"] < threshold,
            )
            for line in page.ocr_lines
        ]
    return PageTextOut(
        page_number=page.page_number, text_source=page.text_source, text=page.normalized_text,
        char_count=page.char_count, embedded_text=page.embedded_text, ocr_text=page.ocr_text,
        ocr_reason=page.ocr_reason, ocr_engine=page.ocr_engine,
        ocr_mean_confidence=page.ocr_mean_confidence, ocr_min_confidence=page.ocr_min_confidence,
        ocr_low_confidence_lines=page.ocr_low_confidence_lines, ocr_lines=lines,
        render_dpi=page.render_dpi, image_width=page.image_width, image_height=page.image_height,
    )


@router.post(
    "/{document_id}/reprocess", response_model=DocumentOut,
    responses={404: {"model": ErrorOut}, 409: {"model": ErrorOut}},
)
def reprocess_document(document_id: int, ingestor: IngestorDep) -> DocumentOut | JSONResponse:
    """Queue a new attempt (all stages) for a failed document, or for one whose AI
    extraction has not run yet. Earlier attempts are kept."""
    try:
        requeue_document(ingestor.session_factory, document_id)
    except RequeueError as exc:
        code = status.HTTP_404_NOT_FOUND if exc.code == "not_found" else status.HTTP_409_CONFLICT
        return _error(code, exc.message, exc.code)
    with ingestor.session_factory() as session:
        document = _load_document(session, document_id)
    assert document is not None
    return document
