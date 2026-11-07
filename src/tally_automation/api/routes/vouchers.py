"""Voucher generation (Phase 7), approval and import (Phase 8) endpoints.

Generation builds and stores TallyPrime import XML on Computer A and sends
nothing. Approval, rejection, revocation and import are separate POSTs; until
login exists (Phase 9) they are accepted only from Computer A itself, and import
additionally needs ``TALLY_WRITE_ENABLED=true``.

Bank account numbers are masked in every response, including inside
transaction descriptions, narrations and the XML preview.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

import ipaddress

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tally_automation.api.deps import SessionDep, SettingsDep
from tally_automation.api.routes.validation import CheckOut, EligibilityOut
from tally_automation.db.models import (
    ApprovalStatus, AttemptStatus, BankStatement, BankTransaction, GeneratedVoucher, ImportStatus, VoucherApproval,
    VoucherImport, VoucherKind, VoucherSource, VoucherStatus,
)
from tally_automation.db.session import transaction
from tally_automation.extraction.masking import mask_in, statement_secrets
from tally_automation.extraction.parsing import mask_account_number
from tally_automation.tally.client import TallyConfigError, create_tally_client
from tally_automation.vouchers import approval
from tally_automation.vouchers.importer import import_voucher
from tally_automation.vouchers.service import (
    InvoiceChoice, StatementChoice, VoucherError, document_vouchers, generate_invoice, generate_statement,
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


class TransactionChoiceIn(BaseModel):
    transaction_id: int
    ledger: LedgerGuid


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


class ApprovalOut(BaseModel):
    decision: str
    xml_sha256: str | None
    warnings_acknowledged: int
    note: str | None
    decided_by_user_id: int | None
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
    client_address: str | None
    started_at: datetime
    finished_at: datetime | None


class HistoryOut(BaseModel):
    approvals: list[ApprovalOut]
    imports: list[ImportAttemptOut]


class PermissionsOut(BaseModel):
    local: bool                      # this browser is on Computer A: decisions and imports are accepted
    write_enabled: bool              # TALLY_WRITE_ENABLED
    tally_configured: bool
    message: str


# ---- Helpers ------------------------------------------------------------------------------

def is_local(request: Request) -> bool:
    """The request comes from Computer A itself: loopback, or the server's own address on both ends."""
    client = request.client.host if request.client else None
    server = request.scope.get("server")
    try:
        address = ipaddress.ip_address(client or "")
    except ValueError:
        return False
    return address.is_loopback or (server is not None and client == server[0])


LOCAL_ONLY = ("Until login exists (Phase 9), vouchers can be approved, rejected and imported only in a browser "
              "on Computer A itself.")


def _local_only(request: Request) -> JSONResponse | None:
    if is_local(request):
        return None
    return JSONResponse(status_code=403, content={"detail": LOCAL_ONLY, "code": "not_local"})


_ERROR_STATUS = {
    "not_found": 404, "no_extraction": 409, "not_eligible": 409, "no_masters": 409, "wrong_type": 409,
    "excluded_transaction": 409, "mapping_incomplete": 422, "unknown_ledger": 422, "unknown_transaction": 422,
    "warnings_not_acknowledged": 422, "write_disabled": 409, "not_configured": 409,
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


# ---- Endpoints --------------------------------------------------------------------------------

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
def generate_vouchers(document_id: int, body: GenerateIn, request: Request, session: SessionDep) -> list[VoucherOut] | JSONResponse:
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
                ), client_address=client)
            else:
                s = body.statement
                assert s is not None
                vouchers = generate_statement(tx, document_id, body.company_guid, StatementChoice(
                    bank=s.bank, voucher_types=dict(s.voucher_types),
                    transactions={t.transaction_id: t.ledger for t in s.transactions},
                ), client_address=client)
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


@router.get("/vouchers/permissions", response_model=PermissionsOut)
def voucher_permissions(request: Request, settings: SettingsDep) -> PermissionsOut:
    local = is_local(request)
    if not local:
        message = LOCAL_ONLY
    elif not settings.tally_write_enabled:
        message = ("Approval is possible. Importing is disabled: set TALLY_WRITE_ENABLED=true in .env on Computer A "
                   "and restart the application.")
    else:
        message = "Approval and import are possible from this browser."
    return PermissionsOut(local=local, write_enabled=settings.tally_write_enabled,
                          tally_configured=bool(settings.tally_host), message=message)


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
    refused = _local_only(request)
    if refused is not None:
        return refused
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
def approve_voucher(voucher_id: int, body: ApproveIn, request: Request, session: SessionDep) -> VoucherOut | JSONResponse:
    """Approve exactly this voucher's XML for import. Sends nothing to TallyPrime."""
    return _decide(request, session, voucher_id, lambda tx, client: approval.approve(
        tx, voucher_id, acknowledge_warnings=body.acknowledge_warnings, note=body.note, client_address=client))


@router.post("/vouchers/{voucher_id}/reject", response_model=VoucherOut, responses={403: {}, 404: {}, 409: {}})
def reject_voucher(voucher_id: int, body: RejectIn, request: Request, session: SessionDep) -> VoucherOut | JSONResponse:
    return _decide(request, session, voucher_id, lambda tx, client: approval.reject(
        tx, voucher_id, note=body.note, client_address=client))


@router.post("/vouchers/{voucher_id}/revoke", response_model=VoucherOut, responses={403: {}, 404: {}, 409: {}})
def revoke_voucher(voucher_id: int, body: RevokeIn, request: Request, session: SessionDep) -> VoucherOut | JSONResponse:
    """Withdraw an approval before import."""
    return _decide(request, session, voucher_id, lambda tx, client: approval.revoke(
        tx, voucher_id, note=body.note, client_address=client))


@router.post("/vouchers/{voucher_id}/import", response_model=ImportAttemptOut, responses={403: {}, 404: {}, 409: {}})
def import_voucher_endpoint(voucher_id: int, body: ImportIn, request: Request, session: SessionDep,
                            settings: SettingsDep) -> ImportAttemptOut | JSONResponse:
    """Create this approved voucher in TallyPrime (after checking it is not there already) and read it back."""
    refused = _local_only(request)
    if refused is not None:
        return refused
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
                                        write_enabled=settings.tally_write_enabled, client_address=client)
    except VoucherError as exc:
        return _error(exc)
    session.expire_all()
    attempt = session.get(VoucherImport, attempt_id)
    assert attempt is not None
    return ImportAttemptOut.model_validate(attempt, from_attributes=True)


@router.get("/vouchers/{voucher_id}/history", response_model=HistoryOut, responses={404: {}})
def voucher_history(voucher_id: int, session: SessionDep) -> HistoryOut | JSONResponse:
    if session.get(GeneratedVoucher, voucher_id) is None:
        return JSONResponse(status_code=404, content={"detail": "Voucher not found.", "code": "not_found"})
    approvals = session.scalars(select(VoucherApproval).where(VoucherApproval.voucher_id == voucher_id)
                                .order_by(VoucherApproval.id)).all()
    imports = session.scalars(select(VoucherImport).where(VoucherImport.voucher_id == voucher_id)
                              .order_by(VoucherImport.attempt)).all()
    return HistoryOut(
        approvals=[ApprovalOut(decision=a.decision.value, xml_sha256=a.xml_sha256,
                               warnings_acknowledged=a.warnings_acknowledged, note=a.note,
                               decided_by_user_id=a.decided_by_user_id, client_address=a.client_address,
                               decided_at=a.decided_at) for a in approvals],
        imports=[ImportAttemptOut.model_validate(i, from_attributes=True) for i in imports],
    )
