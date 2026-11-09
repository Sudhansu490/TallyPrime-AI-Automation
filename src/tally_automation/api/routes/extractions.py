"""Extracted data: read for review, edit field values (audited), and edit history.

Account numbers are masked in every response; full values stay on the server.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from tally_automation.api.deps import SessionDep
from tally_automation.auth.deps import Reviewer
from tally_automation.auth.service import user_names
from tally_automation.db.models import (
    BankStatement, BankTransaction, Document, DocumentType, EditableEntity, ExtractionRun, FieldEdit, Invoice,
    InvoiceLineItem, JobStatus, ProcessingJob, ValidationOutcome,
)
from tally_automation.db.session import transaction
from tally_automation.extraction.masking import mask_in, statement_secrets
from tally_automation.extraction.parsing import ValueType, mask_account_number
from tally_automation.extraction.records import ENTITY_FIELDS, EditError, apply_edit
from tally_automation.extraction.schemas import FieldSpec
from tally_automation.validation.service import validate_document

logger = logging.getLogger(__name__)
router = APIRouter(tags=["extractions"])


def _document_of(entity: Invoice | InvoiceLineItem | BankStatement | BankTransaction) -> int:
    if isinstance(entity, InvoiceLineItem):
        return entity.invoice.document_id
    if isinstance(entity, BankTransaction):
        return entity.statement.document_id
    return entity.document_id

_COUNTED = ("missing", "warning", "invalid")


# ---- Schemas ---------------------------------------------------------------------

class FieldOut(BaseModel):
    name: str
    label: str
    type: str
    value: str | None             # canonical value (Decimal text, ISO date ...) when parsable
    raw: str | None               # text as transcribed / typed
    status: str                   # valid | warning | invalid | missing
    method: str                   # extracted | calculated | table | edited
    flags: list[str]
    messages: list[str]
    evidence: str | None
    page: int | None
    confidence: float | None
    alternatives: list[str]
    edited_at: str | None = None
    original: dict[str, Any] | None = None


class RowOut(BaseModel):
    id: int
    number: int
    row_text: str | None
    page: int | None
    confidence: float
    fields: list[FieldOut]


class EntityOut(BaseModel):
    id: int
    entity: EditableEntity
    fields: list[FieldOut]
    rows: list[RowOut]
    row_entity: EditableEntity
    row_columns: list[dict[str, str]]


class RunOut(BaseModel):
    id: int
    job_id: int
    attempt: int
    provider: str
    model: str
    prompt_version: str
    llm_calls: int
    prompt_tokens: int
    completion_tokens: int
    seconds: float
    created_at: datetime


class SummaryOut(BaseModel):
    fields: int
    missing: int
    warning: int
    invalid: int
    edited: int
    low_confidence: int


class ExtractionOut(BaseModel):
    document_id: int
    document_type: DocumentType
    original_filename: str
    run: RunOut
    document_issues: list[str]
    data: EntityOut
    summary: SummaryOut


class FieldEditIn(BaseModel):
    value: str | None = Field(max_length=2000)
    reason: str | None = Field(default=None, max_length=500)


class EditValidationOut(BaseModel):
    outcome: ValidationOutcome
    error_count: int
    warning_count: int


class FieldEditOut(BaseModel):
    entity: EditableEntity
    entity_id: int
    field: FieldOut
    # Validation re-run after the edit; null if it could not run.
    validation: EditValidationOut | None


class EditRecordOut(BaseModel):
    field_name: str
    old_raw: str | None
    new_raw: str | None
    reason: str | None
    edited_by_user_id: int | None
    edited_by: str | None
    client_address: str | None
    edited_at: datetime


# ---- Serialisation -------------------------------------------------------------------

def _field_out(spec: FieldSpec, meta: dict[str, Any] | None, secrets: tuple[str, ...] = ()) -> FieldOut:
    """Serialise one field. ``secrets``: account numbers to mask wherever they appear,
    because any evidence quote (e.g. for IFSC) can contain the printed account number."""
    meta = dict(meta or {})
    original = dict(meta["original"]) if meta.get("original") else None
    if spec.value_type is ValueType.ACCOUNT_NUMBER:
        for key in ("raw", "value"):
            meta[key] = mask_account_number(meta.get(key))
        meta["alternatives"] = [mask_account_number(a) for a in meta.get("alternatives", [])]
        if original:
            for key in ("raw", "value"):
                original[key] = mask_account_number(original.get(key))
    else:
        for key in ("raw", "value"):
            meta[key] = mask_in(meta.get(key), secrets)
    meta["evidence"] = mask_in(meta.get("evidence"), secrets)
    if original:
        original["evidence"] = mask_in(original.get("evidence"), secrets)
    return FieldOut(
        name=spec.name, label=spec.label, type=spec.value_type.value,
        value=meta.get("value"), raw=meta.get("raw"), status=meta.get("status", "missing"),
        method=meta.get("method", "extracted"), flags=meta.get("flags", []),
        messages=meta.get("messages", []), evidence=meta.get("evidence"), page=meta.get("page"),
        confidence=meta.get("confidence"), alternatives=meta.get("alternatives", []),
        edited_at=meta.get("edited_at"), original=original,
    )


def _fields_out(kind: EditableEntity, fields: dict[str, Any], secrets: tuple[str, ...] = ()) -> list[FieldOut]:
    return [_field_out(spec, fields.get(spec.name), secrets) for spec in ENTITY_FIELDS[kind]]


def _summary(field_lists: list[list[FieldOut]]) -> SummaryOut:
    flat = [f for fields in field_lists for f in fields]
    counts = {name: sum(1 for f in flat if f.status == name) for name in _COUNTED}
    return SummaryOut(
        fields=len(flat), **counts, edited=sum(1 for f in flat if f.method == "edited"),
        low_confidence=sum(1 for f in flat if "low_confidence" in f.flags or "low_ocr_confidence" in f.flags),
    )


def _latest_run(session: Session, document_id: int) -> tuple[ExtractionRun, ProcessingJob] | None:
    row = session.execute(
        select(ExtractionRun, ProcessingJob)
        .join(ProcessingJob, ExtractionRun.job_id == ProcessingJob.id)
        .where(ExtractionRun.document_id == document_id, ProcessingJob.status == JobStatus.SUCCEEDED)
        .order_by(ProcessingJob.attempt.desc()).limit(1)
    ).first()
    return (row[0], row[1]) if row else None


def _columns(kind: EditableEntity) -> list[dict[str, str]]:
    return [{"name": s.name, "label": s.label, "type": s.value_type.value} for s in ENTITY_FIELDS[kind]]


# ---- Endpoints ----------------------------------------------------------------------

@router.get("/documents/{document_id}/extraction", response_model=ExtractionOut)
def get_extraction(document_id: int, session: SessionDep) -> ExtractionOut:
    document = session.get(Document, document_id)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found.")
    found = _latest_run(session, document_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No AI extraction exists for this document yet.")
    run, job = found

    if run.document_type is DocumentType.PURCHASE_INVOICE:
        invoice = session.scalar(
            select(Invoice).options(selectinload(Invoice.line_items)).where(Invoice.extraction_run_id == run.id)
        )
        assert invoice is not None
        kind, row_kind, header = EditableEntity.INVOICE, EditableEntity.INVOICE_LINE_ITEM, invoice
        secrets: tuple[str, ...] = ()
        rows = [
            RowOut(id=i.id, number=i.line_number, row_text=i.row_text, page=i.page, confidence=i.confidence,
                   fields=_fields_out(row_kind, i.fields))
            for i in invoice.line_items
        ]
    else:
        statement = session.scalar(
            select(BankStatement).options(selectinload(BankStatement.transactions))
            .where(BankStatement.extraction_run_id == run.id)
        )
        assert statement is not None
        kind, row_kind, header = EditableEntity.BANK_STATEMENT, EditableEntity.BANK_TRANSACTION, statement
        secrets = statement_secrets(statement)
        rows = [
            RowOut(id=t.id, number=t.sequence, row_text=mask_in(t.row_text, secrets), page=t.page,
                   confidence=t.confidence, fields=_fields_out(row_kind, t.fields, secrets))
            for t in statement.transactions
        ]

    header_fields = _fields_out(kind, header.fields, secrets)
    return ExtractionOut(
        document_id=document.id, document_type=document.document_type,
        original_filename=document.original_filename,
        run=RunOut(
            id=run.id, job_id=job.id, attempt=job.attempt, provider=run.provider, model=run.model,
            prompt_version=run.prompt_version, llm_calls=run.llm_calls, prompt_tokens=run.prompt_tokens,
            completion_tokens=run.completion_tokens, seconds=run.seconds, created_at=run.created_at,
        ),
        document_issues=run.document_issues,
        data=EntityOut(
            id=header.id, entity=kind, fields=header_fields, rows=rows, row_entity=row_kind,
            row_columns=_columns(row_kind),
        ),
        summary=_summary([header_fields, *[r.fields for r in rows]]),
    )


@router.patch(
    "/extractions/{entity}/{entity_id}/fields/{field_name}", response_model=FieldEditOut,
    responses={404: {}, 409: {}, 422: {}},
)
def edit_field(
    entity: EditableEntity, entity_id: int, field_name: str, body: FieldEditIn, request: Request,
    session: SessionDep, user: Reviewer,
) -> FieldEditOut | JSONResponse:
    """Replace one extracted value. The backend re-parses and re-checks it; the
    previous value is kept and the edit is recorded in the audit trail."""
    factory = request.app.state.session_factory
    client = request.client.host if request.client else None
    try:
        with transaction(factory) as tx:
            edited, meta = apply_edit(
                tx, entity, entity_id, field_name, body.value, reason=body.reason, client_address=client,
                user_id=user.id,
            )
            if isinstance(edited, BankStatement):
                secrets = statement_secrets(edited)
            elif isinstance(edited, BankTransaction):
                secrets = statement_secrets(edited.statement)
            else:
                secrets = ()
            # Every edit re-runs validation and duplicate detection for the document.
            # It is plain code; if it fails, the edit is still saved and the
            # validation can be re-run from the Review page.
            tx.flush()
            try:
                with tx.begin_nested():
                    result = validate_document(tx, _document_of(edited))
                    validation = EditValidationOut(
                        outcome=result.outcome, error_count=result.error_count, warning_count=result.warning_count,
                    )
            except Exception:
                logger.exception("validation after edit failed", extra={"entity": entity.value})
                validation = None
    except EditError as exc:
        code = {"not_found": 404, "unknown_field": 404, "unchanged": 409}.get(exc.code, 422)
        return JSONResponse(status_code=code, content={"detail": exc.message, "code": exc.code})
    spec = next(s for s in ENTITY_FIELDS[entity] if s.name == field_name)
    return FieldEditOut(
        entity=entity, entity_id=entity_id, field=_field_out(spec, meta, secrets), validation=validation,
    )


@router.get("/extractions/{entity}/{entity_id}/edits", response_model=list[EditRecordOut])
def list_edits(entity: EditableEntity, entity_id: int, session: SessionDep) -> list[EditRecordOut]:
    edits = session.scalars(
        select(FieldEdit).where(FieldEdit.entity == entity, FieldEdit.entity_id == entity_id)
        .order_by(FieldEdit.id)
    ).all()
    masked = {s.name for s in ENTITY_FIELDS[entity] if s.value_type is ValueType.ACCOUNT_NUMBER}
    names = user_names(session, {e.edited_by_user_id for e in edits})
    return [
        EditRecordOut(
            field_name=e.field_name,
            old_raw=mask_account_number(e.old_raw) if e.field_name in masked else e.old_raw,
            new_raw=mask_account_number(e.new_raw) if e.field_name in masked else e.new_raw,
            reason=e.reason, edited_by_user_id=e.edited_by_user_id, edited_by=names.get(e.edited_by_user_id),
            client_address=e.client_address,
            edited_at=e.edited_at,
        )
        for e in edits
    ]
