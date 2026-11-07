"""Validation results, eligibility, and duplicate management endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from tally_automation.api.deps import SessionDep
from tally_automation.auth.deps import Reviewer
from tally_automation.auth.service import user_names
from tally_automation.db.models import (
    BankTransaction, Document, DocumentStatus, DocumentType, DuplicateKind, DuplicateMatch, Invoice,
    MatchStatus, MatchStrength, ProcessingJob, ReviewDecision, StageStatus, ValidationOutcome, ValidationResult,
)
from tally_automation.db.session import transaction
from tally_automation.extraction.masking import mask_in, statement_secrets
from tally_automation.validation.service import (
    ValidationServiceError, current_run_ids, eligibility, latest_validation, review_match, validate_document,
)

router = APIRouter(tags=["validation"])


# ---- Schemas ---------------------------------------------------------------------------

class CheckOut(BaseModel):
    code: str
    category: str
    severity: Literal["error", "warning", "info"]
    passed: bool
    message: str
    row: int | None
    fields: list[str]
    calculated: str | None
    printed: str | None
    difference: str | None


class EligibilityOut(BaseModel):
    eligible: bool
    reasons: list[str]
    excluded_transaction_ids: list[int]


class ValidationOut(BaseModel):
    document_id: int
    extraction_run_id: int
    rules_version: str
    outcome: ValidationOutcome
    error_count: int
    warning_count: int
    validated_at: datetime
    checks: list[CheckOut]
    eligibility: EligibilityOut
    duplicates: list[MatchSummaryOut]


class DocumentRef(BaseModel):
    id: int
    original_filename: str
    document_type: DocumentType
    uploaded_at: datetime


class ReviewOut(BaseModel):
    decision: ReviewDecision
    note: str | None
    reviewed_by_user_id: int | None
    reviewed_by: str | None
    client_address: str | None
    reviewed_at: datetime


class MatchSummaryOut(BaseModel):
    id: int
    kind: DuplicateKind
    strength: MatchStrength
    status: MatchStatus
    explanation: str
    document_id: int
    other_document_id: int


class MatchOut(MatchSummaryOut):
    rule: str
    document: DocumentRef
    other_document: DocumentRef
    record: str                 # e.g. "Invoice SYN-0001" or "Transaction 3 (p. 1)"
    other_record: str
    evidence: list[dict[str, Any]]
    resolution_note: str | None
    resolved_at: datetime | None
    created_at: datetime
    reviews: list[ReviewOut]


class ExactDuplicateOut(BaseModel):
    document: DocumentRef
    duplicate_of: DocumentRef
    sha256: str


class DuplicateCounts(BaseModel):
    open: int
    duplicate: int
    not_duplicate: int
    exact_files: int


class DuplicatesOut(BaseModel):
    matches: list[MatchOut]
    exact_files: list[ExactDuplicateOut]
    counts: DuplicateCounts


class ReviewIn(BaseModel):
    decision: ReviewDecision
    note: str | None = Field(default=None, max_length=500)


class BulkReviewIn(ReviewIn):
    match_ids: list[int] = Field(min_length=1, max_length=500)


ValidationOut.model_rebuild()


# ---- Helpers ----------------------------------------------------------------------------

def _error(code: int, exc: ValidationServiceError) -> JSONResponse:
    return JSONResponse(status_code=code, content={"detail": exc.message, "code": exc.code})


_ERROR_STATUS = {"not_found": 404, "no_extraction": 409, "already_resolved": 409, "not_resolved": 409}


def _summary(match: DuplicateMatch) -> MatchSummaryOut:
    return MatchSummaryOut(
        id=match.id, kind=match.kind, strength=match.strength, status=match.status,
        explanation=match.explanation, document_id=match.document_id, other_document_id=match.other_document_id,
    )


def _validation_out(session: Session, document_id: int) -> ValidationOut | None:
    result = latest_validation(session, document_id)
    if result is None:
        return None
    matches = session.scalars(
        select(DuplicateMatch).where(
            (DuplicateMatch.document_id == document_id) | (DuplicateMatch.other_document_id == document_id)
        ).order_by(DuplicateMatch.id)
    ).all()
    e = eligibility(session, document_id)
    return ValidationOut(
        document_id=document_id, extraction_run_id=result.extraction_run_id, rules_version=result.rules_version,
        outcome=result.outcome, error_count=result.error_count, warning_count=result.warning_count,
        validated_at=result.validated_at, checks=[CheckOut(**c) for c in result.checks],
        eligibility=EligibilityOut(**e.__dict__), duplicates=[_summary(m) for m in matches],
    )


def _ref(document: Document) -> DocumentRef:
    return DocumentRef(
        id=document.id, original_filename=document.original_filename,
        document_type=document.document_type, uploaded_at=document.created_at,
    )


def _matches_out(session: Session, matches: list[DuplicateMatch]) -> list[MatchOut]:
    document_ids = {m.document_id for m in matches} | {m.other_document_id for m in matches}
    documents = {d.id: d for d in session.scalars(select(Document).where(Document.id.in_(document_ids)))}
    invoice_ids = {i for m in matches if m.kind is DuplicateKind.INVOICE for i in (m.entity_id, m.other_entity_id)}
    invoices = {i.id: i for i in session.scalars(select(Invoice).where(Invoice.id.in_(invoice_ids)))}
    txn_ids = {i for m in matches if m.kind is DuplicateKind.BANK_TRANSACTION for i in (m.entity_id, m.other_entity_id)}
    transactions = {
        t.id: t for t in session.scalars(
            select(BankTransaction).options(selectinload(BankTransaction.statement))
            .where(BankTransaction.id.in_(txn_ids))
        )
    }

    def record(match: DuplicateMatch, entity_id: int) -> str:
        if match.kind is DuplicateKind.INVOICE:
            invoice = invoices.get(entity_id)
            number = invoice.fields.get("invoice_number", {}).get("raw") if invoice else None
            return f"Invoice {number}" if number else f"Invoice record #{entity_id}"
        txn = transactions.get(entity_id)
        if txn is None:
            return f"Transaction record #{entity_id}"
        return f"Transaction {txn.sequence}" + (f" (p. {txn.page})" if txn.page else "")

    names = user_names(session, {r.reviewed_by_user_id for m in matches for r in m.reviews})
    out: list[MatchOut] = []
    for m in matches:
        evidence = m.evidence
        if m.kind is DuplicateKind.BANK_TRANSACTION:
            secrets: tuple[str, ...] = ()
            for entity_id in (m.entity_id, m.other_entity_id):
                if entity_id in transactions:
                    secrets += statement_secrets(transactions[entity_id].statement)
            evidence = [
                {**e, "this": mask_in(e["this"], secrets) if isinstance(e["this"], str) else e["this"],
                 "other": mask_in(e["other"], secrets) if isinstance(e["other"], str) else e["other"]}
                for e in evidence
            ]
        out.append(MatchOut(
            **_summary(m).model_dump(), rule=m.rule,
            document=_ref(documents[m.document_id]), other_document=_ref(documents[m.other_document_id]),
            record=record(m, m.entity_id), other_record=record(m, m.other_entity_id), evidence=evidence,
            resolution_note=m.resolution_note, resolved_at=m.resolved_at, created_at=m.created_at,
            reviews=[ReviewOut(
                decision=r.decision, note=r.note, reviewed_by_user_id=r.reviewed_by_user_id,
                reviewed_by=names.get(r.reviewed_by_user_id), client_address=r.client_address, reviewed_at=r.reviewed_at,
            ) for r in m.reviews],
        ))
    return out


def _load_matches(session: Session, ids: list[int]) -> list[MatchOut]:
    matches = session.scalars(
        select(DuplicateMatch).options(selectinload(DuplicateMatch.reviews))
        .where(DuplicateMatch.id.in_(ids)).order_by(DuplicateMatch.id)
    ).all()
    return _matches_out(session, list(matches))


# ---- Validation endpoints --------------------------------------------------------------------

@router.get(
    "/documents/{document_id}/validation", response_model=ValidationOut, responses={404: {}, 409: {}},
)
def get_validation(document_id: int, session: SessionDep) -> ValidationOut | JSONResponse:
    if session.get(Document, document_id) is None:
        return _error(404, ValidationServiceError("not_found", "Document not found."))
    out = _validation_out(session, document_id)
    if out is None:
        return _error(404, ValidationServiceError(
            "not_validated", "This document has not been validated yet (no extraction, or validation did not run).",
        ))
    return out


@router.post(
    "/documents/{document_id}/validate", response_model=ValidationOut, responses={404: {}, 409: {}},
)
def run_validation(document_id: int, request: Request, session: SessionDep, user: Reviewer) -> ValidationOut | JSONResponse:
    """Re-run validation and duplicate detection on the current extraction (no LLM call)."""
    factory = request.app.state.session_factory
    try:
        with transaction(factory) as tx:
            validate_document(tx, document_id)
            # Recovers a job whose validation stage failed earlier.
            job = tx.scalar(
                select(ProcessingJob).where(ProcessingJob.document_id == document_id)
                .order_by(ProcessingJob.attempt.desc()).limit(1)
            )
            if job is not None and job.validation_status is not StageStatus.SUCCEEDED:
                job.validation_status = StageStatus.SUCCEEDED
    except ValidationServiceError as exc:
        return _error(_ERROR_STATUS.get(exc.code, 409), exc)
    session.expire_all()
    out = _validation_out(session, document_id)
    assert out is not None
    return out


# ---- Duplicate endpoints ----------------------------------------------------------------------

@router.get("/duplicates", response_model=DuplicatesOut)
def list_duplicates(
    session: SessionDep,
    status_filter: Annotated[Literal["open", "resolved", "all"], Query(alias="status")] = "open",
    document_id: int | None = None,
) -> DuplicatesOut:
    query = select(DuplicateMatch.id).order_by(DuplicateMatch.document_id.desc(), DuplicateMatch.id)
    if status_filter == "open":
        query = query.where(DuplicateMatch.status == MatchStatus.OPEN)
    elif status_filter == "resolved":
        query = query.where(DuplicateMatch.status != MatchStatus.OPEN)
    if document_id is not None:
        query = query.where(
            (DuplicateMatch.document_id == document_id) | (DuplicateMatch.other_document_id == document_id)
        )
    matches = _load_matches(session, list(session.scalars(query.limit(1000))))
    exact = session.scalars(
        select(Document).options(selectinload(Document.duplicate_of), selectinload(Document.file))
        .where(Document.status == DocumentStatus.DUPLICATE).order_by(Document.id.desc()).limit(200)
    ).all()
    counts = dict(session.execute(select(DuplicateMatch.status, func.count()).group_by(DuplicateMatch.status)).all())
    exact_count = session.scalar(
        select(func.count()).select_from(Document).where(Document.status == DocumentStatus.DUPLICATE)
    ) or 0
    return DuplicatesOut(
        matches=matches,
        exact_files=[
            ExactDuplicateOut(document=_ref(d), duplicate_of=_ref(d.duplicate_of), sha256=d.file.sha256)
            for d in exact if d.duplicate_of is not None
        ],
        counts=DuplicateCounts(
            open=counts.get(MatchStatus.OPEN, 0), duplicate=counts.get(MatchStatus.DUPLICATE, 0),
            not_duplicate=counts.get(MatchStatus.NOT_DUPLICATE, 0), exact_files=exact_count,
        ),
    )


@router.post("/duplicates/{match_id}/review", response_model=MatchOut, responses={404: {}, 409: {}})
def review_duplicate(match_id: int, body: ReviewIn, request: Request, session: SessionDep, user: Reviewer) -> MatchOut | JSONResponse:
    """Record a decision: ``duplicate`` (the later record is a copy), ``not_duplicate``, or ``reopened``."""
    client = request.client.host if request.client else None
    try:
        with transaction(request.app.state.session_factory) as tx:
            review_match(tx, match_id, body.decision, note=body.note, client_address=client, user_id=user.id)
    except ValidationServiceError as exc:
        return _error(_ERROR_STATUS.get(exc.code, 409), exc)
    session.expire_all()
    return _load_matches(session, [match_id])[0]


@router.post("/duplicates/review", response_model=list[MatchOut], responses={404: {}, 409: {}})
def review_duplicates(body: BulkReviewIn, request: Request, session: SessionDep, user: Reviewer) -> list[MatchOut] | JSONResponse:
    """Apply one decision to several matches (e.g. all transactions of two overlapping
    statements). All or nothing: if any match cannot take the decision, none is changed."""
    client = request.client.host if request.client else None
    ids = list(dict.fromkeys(body.match_ids))
    try:
        with transaction(request.app.state.session_factory) as tx:
            for match_id in ids:
                review_match(tx, match_id, body.decision, note=body.note, client_address=client, user_id=user.id)
    except ValidationServiceError as exc:
        return _error(_ERROR_STATUS.get(exc.code, 409), exc)
    session.expire_all()
    return _load_matches(session, ids)


# ---- Used by the documents API -------------------------------------------------------------------

class DocumentValidationOut(BaseModel):
    outcome: ValidationOutcome
    error_count: int
    warning_count: int
    open_duplicates: int


def document_validations(session: Session, document_ids: list[int]) -> dict[int, DocumentValidationOut]:
    """Validation summary per document for list views (two queries in total)."""
    runs = current_run_ids(session, document_ids)
    if not runs:
        return {}
    results = session.scalars(
        select(ValidationResult).where(ValidationResult.extraction_run_id.in_(runs.values()))
    ).all()
    open_counts: dict[int, int] = {}
    for doc_a, doc_b in session.execute(
        select(DuplicateMatch.document_id, DuplicateMatch.other_document_id)
        .where(DuplicateMatch.status == MatchStatus.OPEN)
    ):
        for doc in (doc_a, doc_b):
            open_counts[doc] = open_counts.get(doc, 0) + 1
    return {
        r.document_id: DocumentValidationOut(
            outcome=r.outcome, error_count=r.error_count, warning_count=r.warning_count,
            open_duplicates=open_counts.get(r.document_id, 0),
        )
        for r in results
    }
