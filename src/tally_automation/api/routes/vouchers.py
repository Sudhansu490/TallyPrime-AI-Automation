"""Voucher generation (Phase 7), approval and import (Phase 8) endpoints.

Generation builds and stores TallyPrime import XML on Computer A and sends
nothing (reviewer role). The pages use one action per document, "approve and
import", which approves every ready voucher and imports it straight away. The
per-voucher approve, reject, revoke and import POSTs remain for the API. All of
them need the approver role (Phase 9); importing additionally needs
``TALLY_WRITE_ENABLED=true``. Every action records the user and the device.

Bank account numbers are masked in every response, including inside
transaction descriptions, narrations and the XML preview.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tally_automation.api.deps import SessionDep, SettingsDep
from tally_automation.auth.deps import Approver, CurrentUserDep, Reviewer
from tally_automation.auth.service import user_names
from tally_automation.api.routes.validation import CheckOut, EligibilityOut
from tally_automation.db.models import (
    AppUser, ApprovalStatus, AttemptStatus, BankStatement, UserRole, BankTransaction, GeneratedVoucher, ImportStatus, VoucherApproval,
    VoucherImport, VoucherKind, VoucherSource, VoucherStatus,
)
from tally_automation.db.session import transaction
from tally_automation.extraction.masking import mask_in, statement_secrets
from tally_automation.extraction.parsing import mask_account_number
from tally_automation.tally.client import TallyConfigError, TallyConnectionError, create_tally_client
from tally_automation.tally.xmlio import TallyResponseError
from tally_automation.vouchers import approval
from tally_automation.vouchers.document_import import approve_and_import_document
from tally_automation.vouchers.importer import import_voucher
from tally_automation.vouchers.tally_entries import find_entered
from tally_automation.vouchers.service import (
    InvoiceChoice, NewLedger, StatementChoice, VoucherError, document_vouchers, generate_invoice, generate_statement,
    prepare, stale_reasons,
)

router = APIRouter(tags=["vouchers"])

LedgerGuid = Annotated[str, Field(min_length=1, max_length=64)]
TypeName = Annotated[str, Field(min_length=1, max_length=255)]


# ---- Schemas ------------------------------------------------------------------------------

class SuggestionOut(BaseModel):
    guid: str | None
    name: str
    reason: str
    parent: str | None
    gstin: str | None


class CompanyOut(BaseModel):
    guid: str
    name: str
    state: str | None
    books_from: date | None
    last_synced_at: datetime | None


class BucketOut(BaseModel):
    key: str
    label: str
    taxable: Decimal


class TaxOut(BaseModel):
    head: str
    amount: Decimal


class InvoiceSetupOut(BaseModel):
    invoice_id: int
    invoice_number: str | None
    invoice_date: date | None
    supplier_name: str | None
    supplier_gstin: str | None
    grand_total: Decimal | None
    taxable: Decimal | None
    round_off: Decimal
    buckets: list[BucketOut]
    taxes: list[TaxOut]
    checks: list[CheckOut]


class TransactionSetupOut(BaseModel):
    id: int
    sequence: int
    date: date | None
    money_out: bool | None
    amount: Decimal | None
    description: str | None          # masked
    reference: str | None            # masked
    excluded: bool
    checks: list[CheckOut]


class StatementSetupOut(BaseModel):
    statement_id: int
    account_number: str | None       # masked
    transactions: list[TransactionSetupOut]


class SetupOut(BaseModel):
    document_id: int
    document_type: str
    original_filename: str
    eligibility: EligibilityOut
    company: CompanyOut | None
    invoice: InvoiceSetupOut | None
    statement: StatementSetupOut | None
    suggestions: dict[str, Any] | None


class InvoiceChoiceIn(BaseModel):
    voucher_type: TypeName
    party: LedgerGuid
    purchase: dict[Annotated[str, Field(max_length=16)], LedgerGuid] = Field(max_length=50)
    duty: dict[Literal["CGST", "SGST", "IGST", "CESS"], LedgerGuid] = Field(default_factory=dict)
    round_off: LedgerGuid | None = None


class NewLedgerIn(BaseModel):
    """A ledger that is not in TallyPrime yet: created there (under ``group``) when the voucher is imported."""
    name: str = Field(min_length=1, max_length=100)
    group: TypeName


class TransactionChoiceIn(BaseModel):
    transaction_id: int
    ledger: LedgerGuid | None = None
    new_ledger: NewLedgerIn | None = None

    @model_validator(mode="after")
    def one_ledger(self) -> "TransactionChoiceIn":
        if (self.ledger is None) == (self.new_ledger is None):
            raise ValueError("Give either an existing ledger or a new ledger for each transaction.")
        return self


class StatementChoiceIn(BaseModel):
    bank: LedgerGuid
    voucher_types: dict[Literal["payment", "receipt", "contra"], TypeName]
    transactions: list[TransactionChoiceIn] = Field(min_length=1, max_length=2000)


class GenerateIn(BaseModel):
    company_guid: LedgerGuid
    invoice: InvoiceChoiceIn | None = None
    statement: StatementChoiceIn | None = None


class EntryOut(BaseModel):
    ledger: str
    ledger_guid: str
    role: str
    debit: Decimal | None
    credit: Decimal | None
    bill: dict[str, Any] | None


class VoucherOut(BaseModel):
    id: int
    document_id: int
    source_kind: VoucherSource
    source_id: int
    source_label: str
    company_name: str
    voucher_kind: VoucherKind
    voucher_type_name: str | None
    voucher_date: date | None
    reference: str | None
    party_ledger_name: str | None
    amount: Decimal | None
    marker: str
    status: VoucherStatus
    error_count: int
    warning_count: int
    checks: list[CheckOut]
    entries: list[EntryOut]
    narration: str | None
    rules_version: str
    generated_at: datetime
    superseded_at: datetime | None
    client_address: str | None
    generated_by: str | None
    stale_reasons: list[str]
    approval_status: ApprovalStatus
    import_status: ImportStatus
    imported_at: datetime | None
    tally_master_id: str | None


class VoucherDetailOut(VoucherOut):
    xml: str | None                  # bank account numbers masked; the stored XML is exact
    xml_sha256: str | None


class ApproveIn(BaseModel):
    acknowledge_warnings: bool = False
    note: str | None = Field(default=None, max_length=500)


class RejectIn(BaseModel):
    note: str = Field(min_length=1, max_length=500)


class RevokeIn(BaseModel):
    note: str | None = Field(default=None, max_length=500)


class ImportIn(BaseModel):
    confirm: Literal[True]           # the page's confirmation dialog; anything else is refused


class DocumentImportIn(BaseModel):
    confirm: Literal[True]           # the page's confirmation dialog; anything else is refused


class DocumentImportItemOut(BaseModel):
    voucher_id: int
    source_label: str
    outcome: Literal["imported", "already_present", "failed", "skipped"]
    message: str


class DocumentImportOut(BaseModel):
    imported: int
    already_present: int
    failed: int
    skipped: int
    message: str
    items: list[DocumentImportItemOut]


class ApprovalOut(BaseModel):
    decision: str
    xml_sha256: str | None
    warnings_acknowledged: int
    note: str | None
    decided_by_user_id: int | None
    decided_by: str | None
    client_address: str | None
    decided_at: datetime


class ImportAttemptOut(BaseModel):
    id: int
    attempt: int
    status: AttemptStatus
    company_name: str
    host: str
    port: int
    xml_sha256: str
    created_count: int | None
    error_count: int | None
    tally_master_id: str | None
    verified: bool | None
    message: str | None
    imported_by_user_id: int | None
    imported_by: str | None = None
    client_address: str | None
    started_at: datetime
    finished_at: datetime | None


class HistoryOut(BaseModel):
    approvals: list[ApprovalOut]
    imports: list[ImportAttemptOut]


class PermissionsOut(BaseModel):
    can_generate: bool               # reviewer role or higher
    can_approve: bool                # approver role or higher: approve, reject, withdraw, import
    write_enabled: bool              # TALLY_WRITE_ENABLED
    tally_configured: bool
    separate_approver: bool          # REQUIRE_SEPARATE_APPROVER
    user_id: int
    message: str


# ---- Helpers ------------------------------------------------------------------------------

_user_names = user_names


_ERROR_STATUS = {
    "not_found": 404, "no_extraction": 409, "not_eligible": 409, "no_masters": 409, "wrong_type": 409,
    "excluded_transaction": 409, "mapping_incomplete": 422, "unknown_ledger": 422, "unknown_transaction": 422,
    "warnings_not_acknowledged": 422, "invalid_new_ledger": 422, "no_vouchers": 409, "write_disabled": 409, "not_configured": 409, "same_person": 403,
}


def _error(exc: VoucherError) -> JSONResponse:
    return JSONResponse(status_code=_ERROR_STATUS.get(exc.code, 409), content={"detail": exc.message, "code": exc.code})


def _secrets(session: Session, voucher: GeneratedVoucher, cache: dict[Any, Any]) -> tuple[str, ...]:
    if voucher.source_kind is not VoucherSource.BANK_TRANSACTION:
        return ()
    key = ("secrets", voucher.extraction_run_id)
    if key not in cache:
        statement = session.scalar(select(BankStatement).where(BankStatement.extraction_run_id == voucher.extraction_run_id))
        cache[key] = statement_secrets(statement) if statement else ()
    return cache[key]


def _source_label(session: Session, voucher: GeneratedVoucher) -> str:
    if voucher.source_kind is VoucherSource.INVOICE:
        return "Invoice"
    txn = session.get(BankTransaction, voucher.source_id)
    return f"Transaction {txn.sequence}" if txn else f"Transaction record #{voucher.source_id}"


def _voucher_out(session: Session, voucher: GeneratedVoucher, cache: dict[Any, Any], *, detail: bool = False) -> VoucherOut:
    secrets = _secrets(session, voucher, cache)
    data: dict[str, Any] = dict(
        id=voucher.id, document_id=voucher.document_id, source_kind=voucher.source_kind, source_id=voucher.source_id,
        source_label=_source_label(session, voucher), company_name=voucher.company_name,
        voucher_kind=voucher.voucher_kind, voucher_type_name=voucher.voucher_type_name,
        voucher_date=voucher.voucher_date, reference=mask_in(voucher.reference, secrets),
        party_ledger_name=voucher.party_ledger_name, amount=voucher.amount, marker=voucher.marker,
        status=voucher.status, error_count=voucher.error_count, warning_count=voucher.warning_count,
        checks=[CheckOut(**c) for c in voucher.checks], entries=[EntryOut(**e) for e in voucher.entries],
        narration=mask_in(voucher.narration, secrets), rules_version=voucher.rules_version,
        generated_at=voucher.generated_at, superseded_at=voucher.superseded_at, client_address=voucher.client_address,
        generated_by=_user_names(session, {voucher.generated_by_user_id}).get(voucher.generated_by_user_id),
        stale_reasons=stale_reasons(session, voucher, cache=cache),
        approval_status=voucher.approval_status, import_status=voucher.import_status,
        imported_at=voucher.imported_at, tally_master_id=voucher.tally_master_id,
    )
    if detail:
        return VoucherDetailOut(**data, xml=mask_in(voucher.xml, secrets), xml_sha256=voucher.xml_sha256)
    return VoucherOut(**data)


def _setup_out(data: dict[str, Any], session: Session, document_id: int) -> SetupOut:
    statement = data.get("statement")
    if statement is not None:
        record = session.get(BankStatement, statement["statement_id"])
        secrets = statement_secrets(record) if record else ()
        statement = {
            **statement, "account_number": mask_account_number(statement["account_number"]),
            "transactions": [
                {**t, "description": mask_in(t["description"], secrets), "reference": mask_in(t["reference"], secrets)}
                for t in statement["transactions"]
            ],
        }
    return SetupOut(**{**data, "statement": statement})


class EnteredOut(BaseModel):
    transaction_id: int
    voucher_type: str | None
    voucher_date: date | None
    number: str | None
    amount: Decimal
    by_this_app: bool


class TallyEntriesOut(BaseModel):
    checked: bool                    # False: TallyPrime could not be asked (not running, company not open…)
    message: str
    entries: list[EnteredOut]


# ---- Endpoints --------------------------------------------------------------------------------

@router.get("/documents/{document_id}/tally-entries", response_model=TallyEntriesOut, responses={404: {}, 409: {}})
def tally_entries(
    document_id: int, request: Request, session: SessionDep, settings: SettingsDep, user: CurrentUserDep,
    company: Annotated[str, Query(min_length=1, max_length=64)],
    bank: Annotated[str, Query(min_length=1, max_length=64)],
) -> TallyEntriesOut | JSONResponse:
    """The statement's transactions TallyPrime already has a voucher for (read only), so they are not
    entered twice."""
    try:
        tally = create_tally_client(settings, transport=getattr(request.app.state, "tally_transport", None))
    except TallyConfigError as exc:
        return TallyEntriesOut(checked=False, message=str(exc), entries=[])
    try:
        with tally:
            found = find_entered(session, tally, document_id, company, bank)
    except VoucherError as exc:
        return _error(exc)
    except (TallyConnectionError, TallyResponseError) as exc:
        return TallyEntriesOut(checked=False, message=f"TallyPrime could not be checked for existing entries: {exc}",
                               entries=[])
    entries = [EnteredOut(**e.__dict__) for e in found]
    message = (f"{len(entries)} transaction(s) are already entered in TallyPrime and are left unticked."
               if entries else "None of these transactions is entered in TallyPrime yet.")
    return TallyEntriesOut(checked=True, message=message, entries=entries)


@router.get("/documents/{document_id}/voucher-setup", response_model=SetupOut, responses={404: {}, 409: {}})
def voucher_setup(
    document_id: int, session: SessionDep,
    company: Annotated[str | None, Query(max_length=64)] = None,
) -> SetupOut | JSONResponse:
    """What the document's voucher(s) need, and ledger suggestions. Reads only this application's data."""
    try:
        data = prepare(session, document_id, company)
    except VoucherError as exc:
        return _error(exc)
    return _setup_out(data, session, document_id)


@router.post("/documents/{document_id}/vouchers", response_model=list[VoucherOut], responses={404: {}, 409: {}, 422: {}})
def generate_vouchers(document_id: int, body: GenerateIn, request: Request, session: SessionDep,
                      user: Reviewer) -> list[VoucherOut] | JSONResponse:
    """Generate (or generate again) the document's voucher(s) with the submitted ledgers. Sends nothing to TallyPrime."""
    client = request.client.host if request.client else None
    if (body.invoice is None) == (body.statement is None):
        return JSONResponse(status_code=422, content={
            "detail": "Send ledger choices for the invoice or for the statement.", "code": "mapping_incomplete"})
    try:
        with transaction(request.app.state.session_factory) as tx:
            if body.invoice is not None:
                c = body.invoice
                vouchers = generate_invoice(tx, document_id, body.company_guid, InvoiceChoice(
                    voucher_type=c.voucher_type, party=c.party, purchase=dict(c.purchase), duty=dict(c.duty),
                    round_off=c.round_off,
                ), client_address=client, user_id=user.id)
            else:
                s = body.statement
                assert s is not None
                vouchers = generate_statement(tx, document_id, body.company_guid, StatementChoice(
                    bank=s.bank, voucher_types=dict(s.voucher_types),
                    transactions={t.transaction_id: t.ledger for t in s.transactions if t.ledger is not None},
                    new_ledgers={t.transaction_id: NewLedger(t.new_ledger.name, t.new_ledger.group)
                                 for t in s.transactions if t.new_ledger is not None},
                ), client_address=client, user_id=user.id)
            ids = [v.id for v in vouchers]
    except VoucherError as exc:
        return _error(exc)
    except IntegrityError:
        return JSONResponse(status_code=409, content={
            "detail": "Vouchers for this document were being generated at the same time. Reload and try again.",
            "code": "generation_conflict"})
    cache: dict[Any, Any] = {}
    return [_voucher_out(session, v, cache) for v in (session.get(GeneratedVoucher, i) for i in ids) if v is not None]


@router.get("/documents/{document_id}/vouchers", response_model=list[VoucherOut])
def list_document_vouchers(
    document_id: int, session: SessionDep, include_superseded: bool = False,
) -> list[VoucherOut]:
    cache: dict[Any, Any] = {}
    return [_voucher_out(session, v, cache)
            for v in document_vouchers(session, document_id, include_superseded=include_superseded)]


class VoucherStatsOut(BaseModel):
    awaiting_approval: int
    approved_not_imported: int
    imported: int
    import_failed: int
    import_unclear: int
    blocked: int


@router.get("/vouchers/stats", response_model=VoucherStatsOut)
def voucher_stats(session: SessionDep) -> VoucherStatsOut:
    current = GeneratedVoucher.status != VoucherStatus.SUPERSEDED

    def count(*conditions) -> int:
        return session.scalar(select(func.count()).select_from(GeneratedVoucher).where(current, *conditions)) or 0

    return VoucherStatsOut(
        awaiting_approval=count(GeneratedVoucher.status == VoucherStatus.READY,
                                GeneratedVoucher.approval_status == ApprovalStatus.PENDING,
                                GeneratedVoucher.import_status == ImportStatus.NOT_IMPORTED),
        approved_not_imported=count(GeneratedVoucher.approval_status == ApprovalStatus.APPROVED,
                                    GeneratedVoucher.import_status == ImportStatus.NOT_IMPORTED),
        imported=count(GeneratedVoucher.import_status == ImportStatus.IMPORTED),
        import_failed=count(GeneratedVoucher.import_status == ImportStatus.FAILED),
        import_unclear=count(GeneratedVoucher.import_status == ImportStatus.UNCERTAIN),
        blocked=count(GeneratedVoucher.status == VoucherStatus.BLOCKED),
    )


@router.get("/vouchers/permissions", response_model=PermissionsOut)
def voucher_permissions(settings: SettingsDep, user: CurrentUserDep) -> PermissionsOut:
    can_approve = user.can(UserRole.APPROVER)
    if not can_approve:
        message = "You can view vouchers. Approving and importing needs the approver role."
    elif not settings.tally_write_enabled:
        message = ("Approving imports into TallyPrime, which is disabled: set TALLY_WRITE_ENABLED=true in .env on "
                   "Computer A and restart the application.")
    else:
        message = "Approve & import creates a document's ready vouchers in TallyPrime in one step."
    if can_approve and settings.require_separate_approver:
        message += " Vouchers you generated must be approved by someone else."
    return PermissionsOut(can_generate=user.can(UserRole.REVIEWER), can_approve=can_approve,
                          write_enabled=settings.tally_write_enabled, tally_configured=bool(settings.tally_host),
                          separate_approver=settings.require_separate_approver, user_id=user.id, message=message)


@router.get("/vouchers", response_model=list[VoucherOut])
def list_vouchers(
    session: SessionDep,
    status: VoucherStatus | None = None,
    approval_status: ApprovalStatus | None = None,
    import_status: ImportStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[VoucherOut]:
    """Current vouchers of all documents (superseded ones only when asked for by status)."""
    query = select(GeneratedVoucher)
    query = query.where(GeneratedVoucher.status == status) if status else query.where(
        GeneratedVoucher.status != VoucherStatus.SUPERSEDED)
    if approval_status:
        query = query.where(GeneratedVoucher.approval_status == approval_status)
    if import_status:
        query = query.where(GeneratedVoucher.import_status == import_status)
    rows = session.scalars(query.order_by(GeneratedVoucher.generated_at.desc(), GeneratedVoucher.id.desc())
                           .limit(limit).offset(offset)).all()
    cache: dict[Any, Any] = {}
    return [_voucher_out(session, v, cache) for v in rows]


@router.get("/vouchers/{voucher_id}", response_model=VoucherDetailOut, responses={404: {}})
def get_voucher(voucher_id: int, session: SessionDep) -> VoucherOut | JSONResponse:
    voucher = session.get(GeneratedVoucher, voucher_id)
    if voucher is None:
        return JSONResponse(status_code=404, content={"detail": "Voucher not found.", "code": "not_found"})
    return _voucher_out(session, voucher, {}, detail=True)


# ---- Approval and import (Phase 8) -------------------------------------------------------------

def _decide(request: Request, session: Session, voucher_id: int, action) -> VoucherOut | JSONResponse:
    client = request.client.host if request.client else None
    try:
        with transaction(request.app.state.session_factory) as tx:
            action(tx, client)
    except VoucherError as exc:
        return _error(exc)
    session.expire_all()
    voucher = session.get(GeneratedVoucher, voucher_id)
    assert voucher is not None
    return _voucher_out(session, voucher, {})


@router.post("/vouchers/{voucher_id}/approve", response_model=VoucherOut, responses={403: {}, 404: {}, 409: {}, 422: {}})
def approve_voucher(voucher_id: int, body: ApproveIn, request: Request, session: SessionDep,
                    settings: SettingsDep, user: Approver) -> VoucherOut | JSONResponse:
    """Approve exactly this voucher's XML for import. Sends nothing to TallyPrime."""
    return _decide(request, session, voucher_id, lambda tx, client: approval.approve(
        tx, voucher_id, acknowledge_warnings=body.acknowledge_warnings, note=body.note, client_address=client,
        user_id=user.id, separate_approver=settings.require_separate_approver))


@router.post("/vouchers/{voucher_id}/reject", response_model=VoucherOut, responses={403: {}, 404: {}, 409: {}})
def reject_voucher(voucher_id: int, body: RejectIn, request: Request, session: SessionDep,
                   user: Approver) -> VoucherOut | JSONResponse:
    return _decide(request, session, voucher_id, lambda tx, client: approval.reject(
        tx, voucher_id, note=body.note, client_address=client, user_id=user.id))


@router.post("/vouchers/{voucher_id}/revoke", response_model=VoucherOut, responses={403: {}, 404: {}, 409: {}})
def revoke_voucher(voucher_id: int, body: RevokeIn, request: Request, session: SessionDep,
                   user: Approver) -> VoucherOut | JSONResponse:
    """Withdraw an approval before import."""
    return _decide(request, session, voucher_id, lambda tx, client: approval.revoke(
        tx, voucher_id, note=body.note, client_address=client, user_id=user.id))


@router.post("/vouchers/{voucher_id}/import", response_model=ImportAttemptOut, responses={403: {}, 404: {}, 409: {}})
def import_voucher_endpoint(voucher_id: int, body: ImportIn, request: Request, session: SessionDep,
                            settings: SettingsDep, user: Approver) -> ImportAttemptOut | JSONResponse:
    """Create this approved voucher in TallyPrime (after checking it is not there already) and read it back."""
    if not settings.tally_write_enabled:
        return _error(VoucherError("write_disabled", "Writing to TallyPrime is disabled. Set TALLY_WRITE_ENABLED=true "
                                                     "in .env on Computer A and restart the application."))
    try:
        tally = create_tally_client(settings, transport=getattr(request.app.state, "tally_transport", None))
    except TallyConfigError as exc:
        return _error(VoucherError("not_configured", str(exc)))
    client = request.client.host if request.client else None
    try:
        with tally:
            attempt_id = import_voucher(request.app.state.session_factory, tally, voucher_id,
                                        write_enabled=settings.tally_write_enabled, client_address=client,
                                        user_id=user.id)
    except VoucherError as exc:
        return _error(exc)
    session.expire_all()
    attempt = session.get(VoucherImport, attempt_id)
    assert attempt is not None
    out = ImportAttemptOut.model_validate(attempt, from_attributes=True)
    out.imported_by = _user_names(session, {attempt.imported_by_user_id}).get(attempt.imported_by_user_id)
    return out


@router.post("/documents/{document_id}/approve-import", response_model=DocumentImportOut,
             responses={403: {}, 404: {}, 409: {}})
def approve_and_import(document_id: int, body: DocumentImportIn, request: Request, session: SessionDep,
                       settings: SettingsDep, user: Approver) -> DocumentImportOut | JSONResponse:
    """Approve every ready voucher of the document and import them into TallyPrime, in one step.

    The approver's click is the approval. Blocked, rejected and out-of-date vouchers are skipped.
    """
    if not settings.tally_write_enabled:
        return _error(VoucherError("write_disabled", "Writing to TallyPrime is disabled. Set TALLY_WRITE_ENABLED=true "
                                                     "in .env on Computer A and restart the application."))
    try:
        tally = create_tally_client(settings, transport=getattr(request.app.state, "tally_transport", None))
    except TallyConfigError as exc:
        return _error(VoucherError("not_configured", str(exc)))
    client = request.client.host if request.client else None
    try:
        with tally:
            result = approve_and_import_document(
                request.app.state.session_factory, tally, document_id, write_enabled=settings.tally_write_enabled,
                client_address=client, user_id=user.id, separate_approver=settings.require_separate_approver)
    except VoucherError as exc:
        return _error(exc)
    session.expire_all()
    items = []
    for i in result.items:
        voucher = session.get(GeneratedVoucher, i.voucher_id)
        label = _source_label(session, voucher) if voucher is not None else f"Voucher {i.voucher_id}"
        items.append(DocumentImportItemOut(voucher_id=i.voucher_id, source_label=label, outcome=i.outcome,
                                           message=i.message))
    done = result.count("imported") + result.count("already_present")
    parts = [f"{done} voucher(s) are now in TallyPrime"]
    if result.count("already_present"):
        parts.append(f"{result.count('already_present')} of them were already there and were not sent again")
    if result.count("failed"):
        parts.append(f"{result.count('failed')} failed (click again to retry)")
    if result.count("skipped"):
        parts.append(f"{result.count('skipped')} skipped")
    if not result.items:
        parts = ["Every voucher of this document is already in TallyPrime"]
    return DocumentImportOut(imported=result.count("imported"), already_present=result.count("already_present"),
                             failed=result.count("failed"), skipped=result.count("skipped"),
                             message="; ".join(parts) + ".", items=items)


@router.get("/vouchers/{voucher_id}/history", response_model=HistoryOut, responses={404: {}})
def voucher_history(voucher_id: int, session: SessionDep) -> HistoryOut | JSONResponse:
    if session.get(GeneratedVoucher, voucher_id) is None:
        return JSONResponse(status_code=404, content={"detail": "Voucher not found.", "code": "not_found"})
    approvals = session.scalars(select(VoucherApproval).where(VoucherApproval.voucher_id == voucher_id)
                                .order_by(VoucherApproval.id)).all()
    imports = session.scalars(select(VoucherImport).where(VoucherImport.voucher_id == voucher_id)
                              .order_by(VoucherImport.attempt)).all()
    names = _user_names(session, {a.decided_by_user_id for a in approvals} | {i.imported_by_user_id for i in imports})
    attempts = []
    for i in imports:
        out = ImportAttemptOut.model_validate(i, from_attributes=True)
        out.imported_by = names.get(i.imported_by_user_id)
        attempts.append(out)
    return HistoryOut(
        approvals=[ApprovalOut(decision=a.decision.value, xml_sha256=a.xml_sha256,
                               warnings_acknowledged=a.warnings_acknowledged, note=a.note,
                               decided_by_user_id=a.decided_by_user_id, decided_by=names.get(a.decided_by_user_id),
                               client_address=a.client_address, decided_at=a.decided_at) for a in approvals],
        imports=attempts,
    )
