"""Running validation and duplicate detection, and deciding eligibility.

``validate_document`` runs after AI extraction, after every reviewer edit and
on request. It (1) runs the extraction and accounting checks on the latest
successful extraction, (2) stores the result, and (3) refreshes the document's
Level 2/3 duplicate matches. Unreviewed matches that no longer apply (e.g.
after an invoice number was corrected) are removed; reviewed ones are kept,
with their audit trail, exactly as decided.

``eligibility`` answers whether a document may go on to voucher generation
(Phase 7). It is computed on demand, never cached, so it cannot go stale.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import delete, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, selectinload

from tally_automation.db.base import utc_now
from tally_automation.db.models import (
    BankStatement, BankTransaction, Document, DocumentStatus, DocumentType, DuplicateKind, DuplicateMatch,
    DuplicateReview, ExtractionRun, Invoice, JobStatus, MatchStatus, ProcessingJob, ReviewDecision,
    ValidationOutcome, ValidationResult,
)
from tally_automation.validation.accounting import (
    RULES_VERSION, Check, Severity, invoice_checks, statement_checks, summarize,
)
from tally_automation.validation.duplicates import (
    Found, InvoiceKeys, TransactionKeys, match_invoice, match_transaction,
)


class ValidationServiceError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---- Current extraction runs ------------------------------------------------------------

def current_run_ids(session: Session, document_ids: list[int] | None = None) -> dict[int, int]:
    """document id → id of its latest successful extraction run."""
    query = (
        select(ExtractionRun.document_id, ExtractionRun.id, ProcessingJob.attempt)
        .join(ProcessingJob, ExtractionRun.job_id == ProcessingJob.id)
        .join(Document, Document.id == ExtractionRun.document_id)
        .where(ProcessingJob.status == JobStatus.SUCCEEDED, Document.status == DocumentStatus.PROCESSED)
    )
    if document_ids is not None:
        query = query.where(ExtractionRun.document_id.in_(document_ids))
    best: dict[int, tuple[int, int]] = {}
    for document_id, run_id, attempt in session.execute(query):
        if document_id not in best or attempt > best[document_id][1]:
            best[document_id] = (run_id, attempt)
    return {document_id: run_id for document_id, (run_id, _) in best.items()}


def _current_run(session: Session, document_id: int) -> ExtractionRun:
    document = session.get(Document, document_id)
    if document is None:
        raise ValidationServiceError("not_found", "Document not found.")
    run_id = current_run_ids(session, [document_id]).get(document_id)
    if run_id is None:
        raise ValidationServiceError(
            "no_extraction", "This document has no successful AI extraction to validate."
        )
    run = session.get(ExtractionRun, run_id)
    assert run is not None
    return run


# ---- Validation ------------------------------------------------------------------------------

def _run_checks(session: Session, run: ExtractionRun, today: date) -> list[Check]:
    if run.document_type is DocumentType.PURCHASE_INVOICE:
        invoice = session.scalar(
            select(Invoice).options(selectinload(Invoice.line_items)).where(Invoice.extraction_run_id == run.id)
        )
        assert invoice is not None
        checks = invoice_checks(invoice.fields, [item.fields for item in invoice.line_items], today=today)
    else:
        statement = session.scalar(
            select(BankStatement).options(selectinload(BankStatement.transactions))
            .where(BankStatement.extraction_run_id == run.id)
        )
        assert statement is not None
        checks = statement_checks(statement.fields, [t.fields for t in statement.transactions], today=today)
    for issue in run.document_issues:
        checks.append(Check("document_issue", "extraction", Severity.WARNING, False, issue))
    return checks


def validate_document(session: Session, document_id: int, *, today: date | None = None) -> ValidationResult:
    """Run all checks on the document's current extraction and refresh its duplicates."""
    run = _current_run(session, document_id)
    checks = _run_checks(session, run, today or date.today())
    errors, warnings = summarize(checks)
    outcome = (ValidationOutcome.FAILED if errors else
               ValidationOutcome.WARNINGS if warnings else ValidationOutcome.PASSED)
    result = session.scalar(select(ValidationResult).where(ValidationResult.extraction_run_id == run.id))
    if result is None:
        result = ValidationResult(extraction_run_id=run.id, document_id=document_id)
        session.add(result)
    result.rules_version = RULES_VERSION
    result.outcome = outcome
    result.error_count = errors
    result.warning_count = warnings
    result.checks = [c.to_json() for c in checks]
    result.validated_at = utc_now()
    detect_duplicates(session, run)
    session.flush()
    return result


def latest_validation(session: Session, document_id: int) -> ValidationResult | None:
    run_id = current_run_ids(session, [document_id]).get(document_id)
    if run_id is None:
        return None
    return session.scalar(select(ValidationResult).where(ValidationResult.extraction_run_id == run_id))


# ---- Duplicate detection ------------------------------------------------------------------------

def _invoice_matches(session: Session, run: ExtractionRun, current: dict[int, int]) -> list[Found]:
    invoice = session.scalar(select(Invoice).where(Invoice.extraction_run_id == run.id))
    assert invoice is not None
    this = InvoiceKeys(invoice.id, run.document_id, invoice.fields)
    conditions = [
        column == value for column, value in (
            (Invoice.supplier_gstin, invoice.supplier_gstin),
            (Invoice.invoice_date, invoice.invoice_date),
            (Invoice.grand_total, invoice.grand_total),
        ) if value is not None
    ]
    other_runs = [run_id for document_id, run_id in current.items() if document_id != run.document_id]
    if not conditions or not other_runs:
        return []
    candidates = session.scalars(
        select(Invoice).where(Invoice.extraction_run_id.in_(other_runs), or_(*conditions))
    ).all()
    found = [match_invoice(this, InvoiceKeys(c.id, c.document_id, c.fields)) for c in candidates]
    return [f for f in found if f is not None]


def _transaction_matches(session: Session, run: ExtractionRun, current: dict[int, int]) -> list[Found]:
    statement = session.scalar(
        select(BankStatement).options(selectinload(BankStatement.transactions))
        .where(BankStatement.extraction_run_id == run.id)
    )
    assert statement is not None
    mine = [TransactionKeys(t.id, run.document_id, statement.account_number, t.fields) for t in statement.transactions]
    for keys, transaction in zip(mine, statement.transactions):
        transaction.fingerprint = keys.fingerprint()
    dates = {k.date for k in mine if k.date is not None}
    other_runs = [run_id for document_id, run_id in current.items() if document_id != run.document_id]
    if statement.account_number is None or not dates or not other_runs:
        return []
    rows = session.execute(
        select(BankTransaction, BankStatement.account_number, BankStatement.document_id)
        .join(BankStatement, BankTransaction.bank_statement_id == BankStatement.id)
        .where(
            BankStatement.extraction_run_id.in_(other_runs),
            BankStatement.account_number == statement.account_number,
            BankTransaction.txn_date.in_(dates),
        )
    ).all()
    by_key: dict[tuple, list[TransactionKeys]] = defaultdict(list)
    for transaction, account, document_id in rows:
        keys = TransactionKeys(transaction.id, document_id, account, transaction.fields)
        by_key[(keys.date, keys.direction_amount)].append(keys)
    found: list[Found] = []
    for keys in mine:
        for other in by_key.get((keys.date, keys.direction_amount), []):
            match = match_transaction(keys, other)
            if match is not None:
                found.append(match)
    return found


def detect_duplicates(session: Session, run: ExtractionRun) -> list[Found]:
    """Refresh the Level 2/3 matches between this document and all other processed documents."""
    current = current_run_ids(session)
    current[run.document_id] = run.id
    if run.document_type is DocumentType.PURCHASE_INVOICE:
        kind, found = DuplicateKind.INVOICE, _invoice_matches(session, run, current)
    else:
        kind, found = DuplicateKind.BANK_TRANSACTION, _transaction_matches(session, run, current)

    document_id = run.document_id
    involving = or_(DuplicateMatch.document_id == document_id, DuplicateMatch.other_document_id == document_id)
    keys = {f.pair_key for f in found}
    # Unreviewed matches that no longer apply are computed state, not decisions: drop them.
    session.execute(
        delete(DuplicateMatch).where(
            involving, DuplicateMatch.kind == kind, DuplicateMatch.status == MatchStatus.OPEN,
            DuplicateMatch.pair_key.not_in(keys) if keys else True,
        ).execution_options(synchronize_session=False)
    )
    existing = {
        m.pair_key: m for m in session.scalars(select(DuplicateMatch).where(DuplicateMatch.pair_key.in_(keys)))
    } if keys else {}
    now = utc_now()
    for f in found:
        newer_is_this = document_id > f.other_document_id
        values = {
            "document_id": document_id if newer_is_this else f.other_document_id,
            "other_document_id": f.other_document_id if newer_is_this else document_id,
            "entity_id": f.entity_id if newer_is_this else f.other_entity_id,
            "other_entity_id": f.other_entity_id if newer_is_this else f.entity_id,
            "strength": f.strength, "rule": f.rule, "explanation": f.explanation, "evidence": f.evidence,
        }
        if not newer_is_this:   # evidence is written from the newer record's point of view
            values["evidence"] = [{**e, "this": e["other"], "other": e["this"]} for e in f.evidence]
        match = existing.get(f.pair_key)
        if match is None:
            # The unique pair key makes concurrent detection safe: a second insert is ignored.
            session.execute(
                sqlite_insert(DuplicateMatch)
                .values(kind=kind, pair_key=f.pair_key, status=MatchStatus.OPEN, created_at=now, updated_at=now, **values)
                .on_conflict_do_nothing(index_elements=["pair_key"])
            )
        elif match.status is MatchStatus.OPEN:
            for name, value in values.items():
                setattr(match, name, value)
    return found


# ---- Eligibility ---------------------------------------------------------------------------------

@dataclass
class Eligibility:
    eligible: bool
    reasons: list[str] = field(default_factory=list)
    excluded_transaction_ids: list[int] = field(default_factory=list)


def eligibility(session: Session, document_id: int) -> Eligibility:
    document = session.get(Document, document_id)
    if document is None:
        raise ValidationServiceError("not_found", "Document not found.")
    if document.status is DocumentStatus.DUPLICATE:
        return Eligibility(False, ["Byte-identical copy of an earlier upload; it is never processed."])
    if document.status is not DocumentStatus.PROCESSED:
        return Eligibility(False, [f"Document is {document.status.value}, not processed."])
    result = latest_validation(session, document_id)
    reasons: list[str] = []
    if result is None:
        reasons.append("Validation has not run on the latest extraction.")
    elif result.outcome is ValidationOutcome.FAILED:
        reasons.append(f"{result.error_count} validation error(s) must be fixed.")
    matches = session.scalars(select(DuplicateMatch).where(
        or_(DuplicateMatch.document_id == document_id, DuplicateMatch.other_document_id == document_id)
    )).all()
    open_count = sum(m.status is MatchStatus.OPEN for m in matches)
    if open_count:
        reasons.append(f"{open_count} possible duplicate(s) have not been reviewed.")
    excluded: list[int] = []
    for m in matches:
        if m.status is MatchStatus.DUPLICATE and m.document_id == document_id:
            if m.kind is DuplicateKind.INVOICE:
                reasons.append(f"Confirmed duplicate of document #{m.other_document_id}.")
            else:
                excluded.append(m.entity_id)
    return Eligibility(not reasons, reasons, sorted(set(excluded)))


def eligible_document_ids(session: Session, document_ids: list[int] | None = None) -> set[int]:
    """Bulk form of ``eligibility`` for counts (three queries instead of several per document).

    Same rules: processed, validated on the current extraction without errors, no
    unreviewed possible duplicate, and not the confirmed later copy of an invoice.
    """
    runs = current_run_ids(session, document_ids)
    if not runs:
        return set()
    outcomes = dict(session.execute(
        select(ValidationResult.extraction_run_id, ValidationResult.outcome)
        .where(ValidationResult.extraction_run_id.in_(runs.values()))
    ).all())
    blocked: set[int] = set()
    for doc, other, status, kind in session.execute(
        select(DuplicateMatch.document_id, DuplicateMatch.other_document_id, DuplicateMatch.status, DuplicateMatch.kind)
        .where(or_(DuplicateMatch.status == MatchStatus.OPEN,
                   (DuplicateMatch.status == MatchStatus.DUPLICATE) & (DuplicateMatch.kind == DuplicateKind.INVOICE)))
    ):
        if status is MatchStatus.OPEN:
            blocked.update((doc, other))
        else:
            blocked.add(doc)       # the newer record of a confirmed duplicate invoice
    return {
        doc for doc, run in runs.items()
        if run in outcomes and outcomes[run] is not ValidationOutcome.FAILED and doc not in blocked
    }


# ---- Reviewer decisions ----------------------------------------------------------------------------

def review_match(
    session: Session, match_id: int, decision: ReviewDecision, *, note: str | None,
    client_address: str | None, user_id: int | None = None,
) -> DuplicateMatch:
    match = session.get(DuplicateMatch, match_id)
    if match is None:
        raise ValidationServiceError("not_found", "Duplicate match not found.")
    if decision is ReviewDecision.REOPENED:
        if match.status is MatchStatus.OPEN:
            raise ValidationServiceError("not_resolved", "This match has not been decided yet.")
        match.status, match.resolved_at, match.resolution_note = MatchStatus.OPEN, None, None
    else:
        if match.status is not MatchStatus.OPEN:
            raise ValidationServiceError(
                "already_resolved", "This match was already decided. Reopen it first to change the decision."
            )
        match.status = MatchStatus(decision.value)
        match.resolved_at = utc_now()
        match.resolution_note = note
    session.add(DuplicateReview(
        match=match, decision=decision, note=note, reviewed_by_user_id=user_id, client_address=client_address,
    ))
    return match
