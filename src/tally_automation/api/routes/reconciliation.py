"""Bank reconciliation endpoints.

Balances are worked out from the statements' reviewed fields on every request, so they always reflect the
latest corrections; statements are never changed here. Accounts are addressed by an opaque key (a hash of
the account number), so account numbers never appear in URLs; responses mask them. Linking an account to a
TallyPrime ledger uses the masters already retrieved and is recorded in the audit trail; nothing is sent to
TallyPrime.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from tally_automation.api.deps import SessionDep, SettingsDep
from tally_automation.auth.deps import CurrentUserDep, Reviewer
from tally_automation.auth.service import user_names
from tally_automation.db.models import ReconciliationAccount, ReconciliationRun, RunKind, TallyCompany
from tally_automation.db.session import transaction
from tally_automation.extraction.parsing import mask_account_number
from tally_automation.reconciliation import balances
from tally_automation.reconciliation.coverage import coverage
from tally_automation.reconciliation.matching import match
from tally_automation.reconciliation.periods import fy_bounds, fy_start_year, month_bounds, parse_fy, parse_month
from tally_automation.reconciliation.reports import build_report, to_pdf, to_xlsx
from tally_automation.reconciliation.service import (
    AccountGroup, ReconciliationError, account_timeline, bank_ledgers, confirmed_duplicates, decide, decisions_for,
    ensure_account, events, group_by_account, ledger_suggestions, link_ledger, load_statements, log_download,
    read_tally, save_run, statement_span, statements_digest, stored_accounts,
)
from tally_automation.reconciliation.transactions import (
    MAX_DAYS, apply_decisions, bank_balance, bank_items, tally_items,
)
from tally_automation.tally.client import TallyConfigError, TallyConnectionError, create_tally_client
from tally_automation.tally.xmlio import TallyResponseError
from tally_automation.validation.accounting import money

router = APIRouter(prefix="/reconciliation", tags=["reconciliation"])

_STATUS = {"not_found": 404, "no_masters": 409, "unknown_ledger": 409, "not_linked": 409, "invalid": 422,
           "not_decided": 409, "already_decided": 409, "not_configured": 409, "tally_error": 502}


def _error(exc: ReconciliationError | ValueError) -> JSONResponse:
    if isinstance(exc, ReconciliationError):
        return JSONResponse(status_code=_STATUS.get(exc.code, 409), content={"detail": exc.message, "code": exc.code})
    return JSONResponse(status_code=422, content={"detail": str(exc), "code": "invalid"})


def _company_names(session) -> dict[str, str]:
    return dict(session.execute(select(TallyCompany.guid, TallyCompany.name)).all())


def _account_out(group: AccountGroup, stored, companies: dict[str, str]) -> dict[str, Any]:
    first, last = statement_span(group.statements)
    ledger = None
    if stored is not None and stored.ledger_guid:
        ledger = {"company_guid": stored.company_guid, "company_name": companies.get(stored.company_guid),
                  "ledger_guid": stored.ledger_guid, "ledger_name": stored.ledger_name,
                  "linked_at": stored.linked_at.isoformat() if stored.linked_at else None}
    banks = sorted({s.bank_name for s in group.statements if s.bank_name})
    return {
        "key": group.key, "account_number": mask_account_number(group.account_number), "bank_names": banks,
        "statement_count": len(group.statements),
        "unusable_count": sum(1 for s in group.statements if not s.usable),
        "unverified_count": sum(1 for s in group.statements if s.unverified),
        "first_day": first.isoformat() if first else None, "last_day": last.isoformat() if last else None,
        "ledger": ledger,
    }


# ---- Accounts ------------------------------------------------------------------------------------

@router.get("/accounts", response_model=None)
def list_accounts(session: SessionDep) -> dict[str, Any]:
    """Every bank account seen on statements, and the statements without an account number."""
    groups, unassigned = group_by_account(load_statements(session))
    stored, companies = stored_accounts(session), _company_names(session)
    accounts = sorted((_account_out(g, stored.get(g.key), companies) for g in groups.values()),
                      key=lambda a: (a["bank_names"], a["account_number"] or ""))
    return {"accounts": accounts, "unassigned": [s.to_json() for s in unassigned]}


@router.get("/accounts/{key}", response_model=None, responses={404: {}})
def account_detail(key: str, session: SessionDep) -> dict[str, Any] | JSONResponse:
    try:
        group, timeline = account_timeline(session, key)
    except ReconciliationError as exc:
        return _error(exc)
    stored = stored_accounts(session).get(key)
    ifsc = next((s.ifsc for s in group.statements if s.ifsc), None)
    history = events(session, key)
    names = user_names(session, {e.user_id for e in history})
    out = _account_out(group, stored, _company_names(session))
    out.update({
        "statements": [s.to_json() for s in sorted(group.statements, key=lambda s: (s.period_from or date.max, s.statement_id))],
        "overlaps": [o.to_json() for o in timeline.overlaps],
        "suggestions": ledger_suggestions(session, group.account_number, ifsc),
        "events": [{"kind": e.kind.value, "detail": e.detail, "note": e.note, "user": names.get(e.user_id),
                    "client_address": e.client_address, "at": e.created_at.isoformat()} for e in history],
    })
    return out


class LinkIn(BaseModel):
    company_guid: str | None = Field(default=None, max_length=64)
    ledger_guid: str | None = Field(default=None, max_length=64)   # None unlinks
    note: str | None = Field(default=None, max_length=500)


@router.post("/accounts/{key}/ledger", response_model=None, responses={404: {}, 409: {}})
def set_ledger(key: str, body: LinkIn, request: Request, session: SessionDep, user: Reviewer) -> dict[str, Any] | JSONResponse:
    """Link the account to a TallyPrime bank ledger, or unlink it (``ledger_guid`` null)."""
    client = request.client.host if request.client else None
    try:
        group, _ = account_timeline(session, key)
        with transaction(request.app.state.session_factory) as tx:
            link_ledger(tx, key, group.account_number, body.company_guid, body.ledger_guid,
                        note=body.note, user_id=user.id, client_address=client)
    except ReconciliationError as exc:
        return _error(exc)
    session.expire_all()
    return _account_out(group, stored_accounts(session).get(key), _company_names(session))


@router.get("/companies/{company_guid}/bank-ledgers", response_model=None, responses={409: {}})
def company_bank_ledgers(company_guid: str, session: SessionDep) -> list[dict[str, Any]] | JSONResponse:
    try:
        ledgers = bank_ledgers(session, company_guid)
    except ReconciliationError as exc:
        return _error(exc)
    return [{"guid": l.guid, "name": l.name, "parent": l.parent,
             "account_number": mask_account_number(l.bank_account_number), "ifsc": l.ifsc} for l in ledgers]


# ---- Balances --------------------------------------------------------------------------------------

def _results(results: list[balances.PeriodResult]) -> dict[str, Any]:
    return {"periods": [r.to_json() for r in results], "summary": balances.summarize(results)}


@router.get("/accounts/{key}/monthly", response_model=None, responses={404: {}, 422: {}})
def monthly(
    key: str, session: SessionDep,
    from_month: Annotated[str | None, Query(alias="from", max_length=7)] = None,
    to_month: Annotated[str | None, Query(alias="to", max_length=7)] = None,
) -> dict[str, Any] | JSONResponse:
    """Each month: opening and closing balance, totals, closing calculated as opening + credits − debits,
    and the opening against the previous month's closing. Default range: the account's statements."""
    try:
        _, timeline = account_timeline(session, key)
        default = balances.default_months(timeline)
        if default is None and (from_month is None or to_month is None):
            return {"periods": [], "summary": {}, "from": None, "to": None}
        first = parse_month(from_month) if from_month else default[0]
        last = parse_month(to_month) if to_month else default[1]
        results = balances.monthly(timeline, first, last)
    except (ReconciliationError, ValueError) as exc:
        return _error(exc)
    return {**_results(results), "from": f"{first[0]:04d}-{first[1]:02d}", "to": f"{last[0]:04d}-{last[1]:02d}"}


@router.get("/accounts/{key}/yearly", response_model=None, responses={404: {}, 422: {}})
def yearly(
    key: str, session: SessionDep,
    fy_from: Annotated[str | None, Query(max_length=7)] = None,
    fy_to: Annotated[str | None, Query(max_length=7)] = None,
) -> dict[str, Any] | JSONResponse:
    """Each financial year (April–March): balances and totals, and its opening on 1 April against the
    previous year's closing on 31 March. Default range: the account's statements."""
    try:
        _, timeline = account_timeline(session, key)
        if timeline.first_day is None and (fy_from is None or fy_to is None):
            return {"periods": [], "summary": {}, "fy_from": None, "fy_to": None}
        first = parse_fy(fy_from) if fy_from else fy_start_year(timeline.first_day)
        last = parse_fy(fy_to) if fy_to else fy_start_year(timeline.last_day)
        results = balances.yearly(timeline, first, last)
    except (ReconciliationError, ValueError) as exc:
        return _error(exc)
    return {**_results(results), "fy_from": results[0].label, "fy_to": results[-1].label}


@router.get("/accounts/{key}/coverage", response_model=None, responses={404: {}, 422: {}})
def account_coverage(
    key: str, session: SessionDep,
    from_date: Annotated[date | None, Query(alias="from")] = None,
    to_date: Annotated[date | None, Query(alias="to")] = None,
) -> dict[str, Any] | JSONResponse:
    """Days, months and financial years that no statement covers (never treated as a zero balance)."""
    try:
        _, timeline = account_timeline(session, key)
        result = coverage(timeline, from_date, to_date)
    except (ReconciliationError, ValueError) as exc:
        return _error(exc)
    out = result.to_json()
    out["unusable_statements"] = [s.to_json() for s in timeline.unusable]
    return out


# ---- Saved runs, decisions and reports -------------------------------------------------------------

KIND_LABELS = {RunKind.MONTHLY: "Monthly balances", RunKind.YEARLY: "Financial-year balances",
               RunKind.TRANSACTIONS: "Transactions with TallyPrime"}


class RunIn(BaseModel):
    kind: RunKind
    month_from: str | None = Field(default=None, max_length=7)       # monthly: YYYY-MM
    month_to: str | None = Field(default=None, max_length=7)
    fy_from: str | None = Field(default=None, max_length=7)          # yearly: 2025-26
    fy_to: str | None = Field(default=None, max_length=7)
    date_from: date | None = None                                     # transactions
    date_to: date | None = None
    window_days: int = Field(default=3, ge=0, le=10)


def _run_summary(run: ReconciliationRun, names: dict[int, str], current_digest: str | None) -> dict[str, Any]:
    return {"id": run.id, "kind": run.kind.value, "label": run.label, "from": run.period_from.isoformat(),
            "to": run.period_to.isoformat(), "ledger_name": run.ledger_name, "created_at": run.created_at.isoformat(),
            "created_by": names.get(run.created_by_user_id),
            "stale": current_digest is not None and current_digest != run.statements_digest}


def _balance_run(body: RunIn, group: AccountGroup, timeline) -> tuple[date, date, str, dict[str, Any]]:
    if body.kind is RunKind.MONTHLY:
        if not body.month_from or not body.month_to:
            raise ValueError("Choose the first and last month.")
        first, last = parse_month(body.month_from), parse_month(body.month_to)
        results = balances.monthly(timeline, first, last)
        start, end = month_bounds(first)[0], month_bounds(last)[1]
        label = f"Monthly {results[0].label} to {results[-1].label}"
    else:
        if not body.fy_from or not body.fy_to:
            raise ValueError("Choose the first and last financial year.")
        first_year, last_year = parse_fy(body.fy_from), parse_fy(body.fy_to)
        results = balances.yearly(timeline, first_year, last_year)
        start, end = fy_bounds(first_year)[0], fy_bounds(last_year)[1]
        label = f"FY {results[0].label} to {results[-1].label}"
    statements = sorted(group.statements, key=lambda s: (s.period_from or date.max, s.statement_id))
    result = {"kind": body.kind.value, "periods": [r.to_json() for r in results],
              "summary": balances.summarize(results), "coverage": coverage(timeline, start, end).to_json(),
              "statements": [s.to_json() for s in statements]}
    return start, end, label, result


def _transaction_result(request: Request, session, settings, stored: ReconciliationAccount | None, timeline,
                        body: RunIn) -> tuple[date, date, str, dict[str, Any]]:
    if stored is None or not stored.ledger_guid:
        raise ReconciliationError("not_linked", "Link this account to its TallyPrime bank ledger first.")
    if body.date_from is None or body.date_to is None or body.date_from > body.date_to:
        raise ValueError("Choose a start date on or before the end date.")
    if (body.date_to - body.date_from).days + 1 > MAX_DAYS:
        raise ValueError(f"Compare at most {MAX_DAYS} days at a time.")
    company = session.scalar(select(TallyCompany).where(TallyCompany.guid == stored.company_guid))
    if company is None:
        raise ReconciliationError("no_masters", "The linked company's masters are no longer stored. "
                                                "Retrieve them on the TallyPrime page.")
    start, end = body.date_from, body.date_to
    try:
        with create_tally_client(settings, transport=getattr(request.app.state, "tally_transport", None)) as client:
            side = read_tally(client, company, stored.ledger_name, start, end)
    except TallyConfigError as exc:
        raise ReconciliationError("not_configured", str(exc)) from None
    except (TallyConnectionError, TallyResponseError) as exc:
        raise ReconciliationError("tally_error", f"TallyPrime could not be read: {exc}") from None
    bank, left_out, unreadable = bank_items(timeline, start, end, confirmed_duplicates(session))
    tally = tally_items(side.vouchers, stored.ledger_name)
    matched = match(bank, tally, window_days=body.window_days)
    result = {
        "kind": "transactions", "from": start.isoformat(), "to": end.isoformat(),
        "window_days": body.window_days, "company_name": side.company_name,
        "matches": [m.to_json() for m in matched.matches],
        "bank_only": [b.to_json() for b in matched.bank_only],
        "tally_only": [t.to_json() for t in matched.tally_only],
        "excluded": left_out, "unreadable": unreadable,
        "bank_transactions": len(bank), "tally_entries": len(tally),
        "balances": {"bank_opening": bank_balance(timeline, start, start_of_day=True).to_json(),
                     "bank_closing": bank_balance(timeline, end, start_of_day=False).to_json(),
                     "tally_opening": money(side.opening), "tally_closing": money(side.closing)},
        "tally_notes": side.notes,
    }
    return start, end, f"Transactions {start:%d %b %Y} to {end:%d %b %Y}", result


@router.post("/accounts/{key}/runs", response_model=None, responses={404: {}, 409: {}, 422: {}, 502: {}})
def create_run(key: str, body: RunIn, request: Request, session: SessionDep, settings: SettingsDep,
               user: Reviewer) -> dict[str, Any] | JSONResponse:
    """Work out a reconciliation and save it exactly as it is, for review and reports. A transaction
    comparison reads the period's vouchers and the bank ledger's balances from TallyPrime (read only)."""
    client_address = request.client.host if request.client else None
    try:
        group, timeline = account_timeline(session, key)
        digest = statements_digest(group.statements)
        if body.kind is RunKind.TRANSACTIONS:
            start, end, label, result = _transaction_result(
                request, session, settings, stored_accounts(session).get(key), timeline, body)
        else:
            start, end, label, result = _balance_run(body, group, timeline)
        with transaction(request.app.state.session_factory) as tx:
            account = ensure_account(tx, key, group.account_number)
            run = save_run(tx, account, body.kind, start, end, label, body.model_dump(mode="json", exclude_none=True),
                           result, digest, user_id=user.id, client_address=client_address)
            run_id = run.id
    except (ReconciliationError, ValueError) as exc:
        return _error(exc)
    session.expire_all()
    return _run_view(session, session.get(ReconciliationRun, run_id))


@router.get("/accounts/{key}/runs", response_model=None, responses={404: {}})
def list_runs(key: str, session: SessionDep,
              limit: Annotated[int, Query(ge=1, le=100)] = 30) -> list[dict[str, Any]] | JSONResponse:
    try:
        group, _ = account_timeline(session, key)
    except ReconciliationError as exc:
        return _error(exc)
    account = stored_accounts(session).get(key)
    if account is None:
        return []
    runs = session.scalars(select(ReconciliationRun).where(ReconciliationRun.account_id == account.id)
                           .order_by(ReconciliationRun.id.desc()).limit(limit)).all()
    names = user_names(session, {r.created_by_user_id for r in runs})
    digest = statements_digest(group.statements)
    return [_run_summary(r, names, digest) for r in runs]


def _run_context(session, run: ReconciliationRun) -> tuple[ReconciliationAccount, AccountGroup | None]:
    account = session.get(ReconciliationAccount, run.account_id)
    groups, _ = group_by_account(load_statements(session))
    return account, groups.get(account.account_key)


def _run_view(session, run: ReconciliationRun | None) -> dict[str, Any] | JSONResponse:
    if run is None:
        return JSONResponse(status_code=404, content={"detail": "Reconciliation not found.", "code": "not_found"})
    account, group = _run_context(session, run)
    digest = statements_digest(group.statements) if group else None
    names = user_names(session, {run.created_by_user_id})
    result = run.result
    if run.kind is RunKind.TRANSACTIONS:
        result = apply_decisions(result, decisions_for(session, account.id))
    return {**_run_summary(run, names, digest), "account_key": account.account_key,
            "account_number": mask_account_number(account.account_number),
            "bank_names": sorted({s.bank_name for s in (group.statements if group else []) if s.bank_name}),
            "result": result}


@router.get("/runs/{run_id}", response_model=None, responses={404: {}})
def get_run(run_id: int, session: SessionDep) -> dict[str, Any] | JSONResponse:
    return _run_view(session, session.get(ReconciliationRun, run_id))


class DecisionIn(BaseModel):
    decision: Literal["approved", "rejected", "reopened"]
    note: str | None = Field(default=None, max_length=500)


@router.post("/runs/{run_id}/matches/{signature}", response_model=None, responses={404: {}, 409: {}})
def decide_match(run_id: int, signature: str, body: DecisionIn, request: Request, session: SessionDep,
                 user: Reviewer) -> dict[str, Any] | JSONResponse:
    """Approve or reject a proposed match, or reopen it. The decision applies to this pairing of entries in
    every comparison of the account, and is recorded in the history."""
    client_address = request.client.host if request.client else None
    try:
        with transaction(request.app.state.session_factory) as tx:
            run = tx.get(ReconciliationRun, run_id)
            if run is None:
                raise ReconciliationError("not_found", "Reconciliation not found.")
            decide(tx, run, signature[:64], body.decision, note=body.note, user_id=user.id,
                   client_address=client_address)
    except ReconciliationError as exc:
        return _error(exc)
    session.expire_all()
    return _run_view(session, session.get(ReconciliationRun, run_id))


_MEDIA = {"xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "pdf": "application/pdf"}


@router.get("/runs/{run_id}/report.{file_format}", response_model=None, responses={404: {}})
def download_report(run_id: int, file_format: Literal["xlsx", "pdf"], request: Request, session: SessionDep,
                    user: CurrentUserDep) -> Response:
    """The saved reconciliation as an Excel workbook or a PDF. The download is recorded in the history."""
    run = session.get(ReconciliationRun, run_id)
    view = _run_view(session, run)
    if isinstance(view, JSONResponse):
        return view
    company = (session.scalar(select(TallyCompany.name).where(TallyCompany.guid == run.company_guid))
               if run.company_guid else None)
    meta = {"account_number": view["account_number"], "bank_names": view["bank_names"], "label": run.label,
            "kind_label": KIND_LABELS[run.kind], "period_from": run.period_from.isoformat(),
            "period_to": run.period_to.isoformat(), "created_at": run.created_at.strftime("%d %b %Y %H:%M UTC"),
            "created_by": view["created_by"], "generated_at": datetime.now(UTC).strftime("%d %b %Y %H:%M UTC"),
            "stale": view["stale"], "ledger_name": run.ledger_name, "company_name": company}
    report = build_report(meta, view["result"])
    content = to_xlsx(report) if file_format == "xlsx" else to_pdf(report)
    with transaction(request.app.state.session_factory) as tx:
        log_download(tx, tx.get(ReconciliationRun, run_id), file_format, user_id=user.id,
                     client_address=request.client.host if request.client else None)
    filename = f"bank-reconciliation-{run.id}-{run.kind.value}.{file_format}"
    return Response(content, media_type=_MEDIA[file_format],
                    headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"})
