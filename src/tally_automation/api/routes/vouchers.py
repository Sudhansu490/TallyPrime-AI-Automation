"""Voucher generation endpoints (Phase 7).

Generation builds and stores TallyPrime import XML on Computer A. Nothing is
sent to TallyPrime: there is no import endpoint in this phase, and importing
will be a separate operation that requires approval (Phase 8).

Bank account numbers are masked in every response, including inside
transaction descriptions, narrations and the XML preview.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tally_automation.api.deps import SessionDep
from tally_automation.api.routes.validation import CheckOut, EligibilityOut
from tally_automation.db.models import (
    BankStatement, BankTransaction, GeneratedVoucher, VoucherKind, VoucherSource, VoucherStatus,
)
from tally_automation.db.session import transaction
from tally_automation.extraction.masking import mask_in, statement_secrets
from tally_automation.extraction.parsing import mask_account_number
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


class VoucherDetailOut(VoucherOut):
    xml: str | None                  # bank account numbers masked; the stored XML is exact
    xml_sha256: str | None


# ---- Helpers ------------------------------------------------------------------------------

_ERROR_STATUS = {
    "not_found": 404, "no_extraction": 409, "not_eligible": 409, "no_masters": 409, "wrong_type": 409,
    "excluded_transaction": 409, "mapping_incomplete": 422, "unknown_ledger": 422, "unknown_transaction": 422,
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


@router.get("/vouchers", response_model=list[VoucherOut])
def list_vouchers(
    session: SessionDep,
    status: VoucherStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[VoucherOut]:
    """Current vouchers of all documents (superseded ones only when asked for by status)."""
    query = select(GeneratedVoucher)
    query = query.where(GeneratedVoucher.status == status) if status else query.where(
        GeneratedVoucher.status != VoucherStatus.SUPERSEDED)
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
