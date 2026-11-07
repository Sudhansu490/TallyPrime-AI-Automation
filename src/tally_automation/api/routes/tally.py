"""TallyPrime integration endpoints (Phase 6, read-only).

The browser never talks to TallyPrime: these endpoints do, from Computer A.
``POST /tally/sync`` is a POST only because it changes *this* application's
stored snapshot; towards TallyPrime it sends export (read) requests only.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from tally_automation.api.deps import SessionDep, SettingsDep
from tally_automation.auth.deps import Reviewer
from tally_automation.db.models import (
    SyncStatus, TallyCompany, TallyGroup, TallyLedger, TallySyncRun, TallyVoucherType,
)
from tally_automation.extraction.parsing import mask_account_number
from tally_automation.tally.client import TallyConfigError, create_tally_client
from tally_automation.tally.service import SyncError, check_connection, sync_company

router = APIRouter(prefix="/tally", tags=["tally"])


# ---- Schemas -------------------------------------------------------------------------

class CompanyOut(BaseModel):
    name: str
    guid: str
    books_from: date | None
    state: str | None


class SyncRunOut(BaseModel):
    id: int
    company_name: str
    company_guid: str | None
    status: SyncStatus
    group_count: int | None
    ledger_count: int | None
    voucher_type_count: int | None
    removed_characters: int | None
    warnings: list[str]
    error_message: str | None
    started_at: datetime
    finished_at: datetime | None


class StatusOut(BaseModel):
    configured: bool
    host: str | None
    port: int
    write_enabled: bool               # always reported; nothing in Phase 6 can write
    read_only: bool                   # this build sends export requests only
    reachable: bool
    server_running: bool
    message: str
    milliseconds: int | None
    checked_at: datetime | None
    loaded_companies: list[CompanyOut]
    default_company: str | None
    last_sync: SyncRunOut | None


class StoredCompanyOut(CompanyOut):
    last_synced_at: datetime | None
    last_sync_run_id: int | None
    group_count: int
    ledger_count: int
    voucher_type_count: int


class SyncIn(BaseModel):
    company: str = Field(min_length=1, max_length=255)


class GroupOut(BaseModel):
    name: str
    aliases: list[str]
    parent: str | None
    primary_group: str | None
    is_revenue: bool | None
    is_deemed_positive: bool | None


class LedgerOut(BaseModel):
    guid: str
    name: str
    aliases: list[str]
    parent: str | None
    primary_group: str | None
    gstin: str | None
    gstin_valid: bool | None
    gst_registration_type: str | None
    state: str | None
    tax_type: str | None
    gst_duty_head: str | None
    tax_rate: Decimal | None
    bank_account_number: str | None   # masked
    ifsc: str | None
    is_bill_wise: bool | None


class LedgerList(BaseModel):
    items: list[LedgerOut]
    total: int
    limit: int
    offset: int


class VoucherTypeOut(BaseModel):
    name: str
    aliases: list[str]
    parent: str | None
    numbering_method: str | None
    is_active: bool | None


# ---- Helpers ------------------------------------------------------------------------------

def _error(status_code: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail, "code": code})


def _run_out(run: TallySyncRun) -> SyncRunOut:
    return SyncRunOut(
        id=run.id, company_name=run.company_name, company_guid=run.company_guid, status=run.status,
        group_count=run.group_count, ledger_count=run.ledger_count, voucher_type_count=run.voucher_type_count,
        removed_characters=run.removed_characters, warnings=run.warnings or [], error_message=run.error_message,
        started_at=run.started_at, finished_at=run.finished_at,
    )


def _client(request: Request, settings):
    return create_tally_client(settings, transport=getattr(request.app.state, "tally_transport", None))


# ---- Endpoints ---------------------------------------------------------------------------

@router.get("/status", response_model=StatusOut)
def tally_status(request: Request, session: SessionDep, settings: SettingsDep) -> StatusOut:
    """Live check: is TallyPrime reachable, and which companies are loaded?"""
    last = session.scalar(select(TallySyncRun).order_by(TallySyncRun.id.desc()).limit(1))
    base = {
        "configured": bool(settings.tally_host), "host": settings.tally_host, "port": settings.tally_port,
        "write_enabled": settings.tally_write_enabled, "read_only": True, "default_company": settings.tally_company,
        "last_sync": _run_out(last) if last else None,
    }
    try:
        client = _client(request, settings)
    except TallyConfigError as exc:
        return StatusOut(**base, reachable=False, server_running=False, message=str(exc), milliseconds=None,
                         checked_at=None, loaded_companies=[])
    with client:
        check = check_connection(client)
    return StatusOut(
        **base, reachable=check.reachable, server_running=check.server_running, message=check.message,
        milliseconds=check.milliseconds, checked_at=check.checked_at,
        loaded_companies=[CompanyOut(name=c.name, guid=c.guid, books_from=c.books_from, state=c.state)
                          for c in check.companies],
    )


@router.post("/sync", response_model=SyncRunOut, responses={409: {}, 502: {}})
def tally_sync(body: SyncIn, request: Request, session: SessionDep, settings: SettingsDep,
               user: Reviewer) -> SyncRunOut | JSONResponse:
    """Retrieve groups, ledgers and voucher types of a loaded company (read-only)."""
    try:
        client = _client(request, settings)
    except TallyConfigError as exc:
        return _error(409, "not_configured", str(exc))
    try:
        with client:
            run_id = sync_company(request.app.state.session_factory, client, body.company, user.id)
    except SyncError as exc:
        status_code = 502 if exc.code == "tally_error" else 409
        return _error(status_code, exc.code, exc.message)
    session.expire_all()
    run = session.get(TallySyncRun, run_id)
    assert run is not None
    return _run_out(run)


@router.get("/sync-runs", response_model=list[SyncRunOut])
def tally_sync_runs(session: SessionDep, limit: Annotated[int, Query(ge=1, le=100)] = 20) -> list[SyncRunOut]:
    runs = session.scalars(select(TallySyncRun).order_by(TallySyncRun.id.desc()).limit(limit)).all()
    return [_run_out(r) for r in runs]


@router.get("/companies", response_model=list[StoredCompanyOut])
def tally_companies(session: SessionDep) -> list[StoredCompanyOut]:
    """Companies whose masters have been retrieved at least once."""
    def counts(model) -> dict[str, int]:
        return dict(session.execute(select(model.company_guid, func.count()).group_by(model.company_guid)).all())

    groups, ledgers, voucher_types = counts(TallyGroup), counts(TallyLedger), counts(TallyVoucherType)
    return [
        StoredCompanyOut(
            name=c.name, guid=c.guid, books_from=c.books_from, state=c.state, last_synced_at=c.last_synced_at,
            last_sync_run_id=c.last_sync_run_id, group_count=groups.get(c.guid, 0),
            ledger_count=ledgers.get(c.guid, 0), voucher_type_count=voucher_types.get(c.guid, 0),
        )
        for c in session.scalars(select(TallyCompany).order_by(TallyCompany.name))
    ]


def _company_known(session, guid: str) -> bool:
    return session.scalar(select(func.count()).select_from(TallyCompany).where(TallyCompany.guid == guid)) > 0


@router.get("/companies/{company_guid}/groups", response_model=list[GroupOut], responses={404: {}})
def tally_groups(company_guid: str, session: SessionDep) -> list[GroupOut] | JSONResponse:
    if not _company_known(session, company_guid):
        return _error(404, "not_found", "No retrieved masters for this company.")
    rows = session.scalars(select(TallyGroup).where(TallyGroup.company_guid == company_guid).order_by(TallyGroup.name))
    return [GroupOut(name=g.name, aliases=g.aliases, parent=g.parent, primary_group=g.primary_group,
                     is_revenue=g.is_revenue, is_deemed_positive=g.is_deemed_positive) for g in rows]


@router.get("/companies/{company_guid}/ledgers", response_model=LedgerList, responses={404: {}})
def tally_ledgers(
    company_guid: str, session: SessionDep,
    q: Annotated[str | None, Query(max_length=100)] = None,
    group: Annotated[str | None, Query(max_length=255)] = None,
    kind: Annotated[str | None, Query(pattern="^(gst_party|gst_duty|bank|invalid_gstin)$")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> LedgerList | JSONResponse:
    if not _company_known(session, company_guid):
        return _error(404, "not_found", "No retrieved masters for this company.")
    conditions = [TallyLedger.company_guid == company_guid]
    if q:
        like = f"%{q.strip()}%"
        conditions.append(or_(TallyLedger.name.ilike(like), TallyLedger.gstin.ilike(like)))
    if group:
        conditions.append(or_(TallyLedger.parent == group, TallyLedger.primary_group == group))
    if kind == "gst_party":
        conditions.append(TallyLedger.gstin.is_not(None))
    elif kind == "gst_duty":
        conditions.append(TallyLedger.gst_duty_head.is_not(None))
    elif kind == "bank":
        conditions.append(or_(TallyLedger.bank_account_number.is_not(None), TallyLedger.primary_group == "Bank Accounts",
                              TallyLedger.parent == "Bank Accounts"))
    elif kind == "invalid_gstin":
        conditions.append(TallyLedger.gstin_valid.is_(False))
    total = session.scalar(select(func.count()).select_from(TallyLedger).where(*conditions)) or 0
    rows = session.scalars(
        select(TallyLedger).where(*conditions).order_by(TallyLedger.name).limit(limit).offset(offset)
    ).all()
    return LedgerList(
        items=[LedgerOut(
            guid=l.guid, name=l.name, aliases=l.aliases, parent=l.parent, primary_group=l.primary_group, gstin=l.gstin,
            gstin_valid=l.gstin_valid, gst_registration_type=l.gst_registration_type, state=l.state,
            tax_type=l.tax_type, gst_duty_head=l.gst_duty_head, tax_rate=l.tax_rate,
            bank_account_number=mask_account_number(l.bank_account_number), ifsc=l.ifsc, is_bill_wise=l.is_bill_wise,
        ) for l in rows],
        total=total, limit=limit, offset=offset,
    )


@router.get("/companies/{company_guid}/voucher-types", response_model=list[VoucherTypeOut], responses={404: {}})
def tally_voucher_types(company_guid: str, session: SessionDep) -> list[VoucherTypeOut] | JSONResponse:
    if not _company_known(session, company_guid):
        return _error(404, "not_found", "No retrieved masters for this company.")
    rows = session.scalars(
        select(TallyVoucherType).where(TallyVoucherType.company_guid == company_guid).order_by(TallyVoucherType.name)
    )
    return [VoucherTypeOut(name=v.name, aliases=v.aliases, parent=v.parent, numbering_method=v.numbering_method,
                           is_active=v.is_active) for v in rows]
