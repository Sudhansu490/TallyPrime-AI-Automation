"""Bank reconciliation endpoints.

Balances are worked out from the statements' reviewed fields on every request, so they always reflect the
latest corrections; statements are never changed here. Accounts are addressed by an opaque key (a hash of
the account number), so account numbers never appear in URLs; responses mask them. Linking an account to a
TallyPrime ledger uses the masters already retrieved and is recorded in the audit trail; nothing is sent to
TallyPrime.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from tally_automation.api.deps import SessionDep
from tally_automation.auth.deps import Reviewer
from tally_automation.auth.service import user_names
from tally_automation.db.models import TallyCompany
from tally_automation.db.session import transaction
from tally_automation.extraction.parsing import mask_account_number
from tally_automation.reconciliation import balances
from tally_automation.reconciliation.coverage import coverage
from tally_automation.reconciliation.periods import fy_start_year, parse_fy, parse_month
from tally_automation.reconciliation.service import (
    AccountGroup, ReconciliationError, account_timeline, bank_ledgers, events, group_by_account, ledger_suggestions,
    link_ledger, load_statements, statement_span, stored_accounts,
)

router = APIRouter(prefix="/reconciliation", tags=["reconciliation"])

_STATUS = {"not_found": 404, "no_masters": 409, "unknown_ledger": 409, "not_linked": 409, "invalid": 422}


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
