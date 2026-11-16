"""Bank reconciliation: reading statements from the database, and linking accounts to TallyPrime ledgers.

Statements are read from each document's current extraction (the same one the Review and Generate pages
use), with the reviewer's corrections. Nothing here changes a statement, and nothing is sent to TallyPrime:
the ledger list comes from the masters already retrieved on the TallyPrime page.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from tally_automation.db.models import (
    BankStatement, Document, DocumentType, MappingMemory, MappingPurpose, ReconciliationAccount,
    ReconciliationEvent, ReconciliationEventKind, TallyCompany, TallyLedger, ValidationResult,
)
from tally_automation.db.base import utc_now
from tally_automation.reconciliation.balances import StatementData, Timeline, build_timeline, prepare_statement
from tally_automation.validation.service import current_run_ids
from tally_automation.vouchers import matching
from tally_automation.vouchers.service import VoucherError, load_masters


class ReconciliationError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def normalize_account(value: str | None) -> str | None:
    """The account number as printed, without spaces or dashes. Masked digits (X/*) are kept, so a
    masked and a full number are different accounts: they are never assumed to be the same."""
    compact = re.sub(r"[\s\-]", "", value or "").upper()
    return compact or None


def account_key(normalized: str) -> str:
    return hashlib.sha256(f"reconciliation-account:{normalized}".encode()).hexdigest()


# ---- Statements ----------------------------------------------------------------------------------

def load_statements(session: Session) -> list[StatementData]:
    """Every bank statement's current extraction, prepared for reconciliation."""
    run_ids = current_run_ids(session)
    if not run_ids:
        return []
    statements = session.scalars(
        select(BankStatement).options(selectinload(BankStatement.transactions))
        .where(BankStatement.extraction_run_id.in_(run_ids.values()))
    ).all()
    if not statements:
        return []
    outcomes = dict(session.execute(
        select(ValidationResult.extraction_run_id, ValidationResult.outcome)
        .where(ValidationResult.extraction_run_id.in_([s.extraction_run_id for s in statements]))
    ).all())
    names = dict(session.execute(
        select(Document.id, Document.original_filename)
        .where(Document.id.in_([s.document_id for s in statements]), Document.document_type == DocumentType.BANK_STATEMENT)
    ).all())
    out = []
    for s in statements:
        if s.document_id not in names:
            continue
        outcome = outcomes.get(s.extraction_run_id)
        out.append(prepare_statement(
            s.id, s.document_id, names[s.document_id], s.fields,
            [t.fields for t in sorted(s.transactions, key=lambda t: t.sequence)],
            outcome.value if outcome is not None else None,
        ))
    return out


@dataclass
class AccountGroup:
    key: str
    account_number: str
    statements: list[StatementData] = field(default_factory=list)


def group_by_account(statements: list[StatementData]) -> tuple[dict[str, AccountGroup], list[StatementData]]:
    """Statements per account, and the statements without a usable account number."""
    groups: dict[str, AccountGroup] = {}
    unassigned = []
    for stmt in statements:
        normalized = normalize_account(stmt.account_number)
        if normalized is None:
            unassigned.append(stmt)
            continue
        key = account_key(normalized)
        groups.setdefault(key, AccountGroup(key, normalized)).statements.append(stmt)
    return groups, unassigned


def account_timeline(session: Session, key: str) -> tuple[AccountGroup, Timeline]:
    groups, _ = group_by_account(load_statements(session))
    group = groups.get(key)
    if group is None:
        raise ReconciliationError("not_found", "No bank statements for this account.")
    return group, build_timeline(group.statements)


# ---- Ledger links ----------------------------------------------------------------------------------

def stored_accounts(session: Session) -> dict[str, ReconciliationAccount]:
    return {a.account_key: a for a in session.scalars(select(ReconciliationAccount))}


def ledger_suggestions(session: Session, account_number: str, ifsc: str | None) -> list[dict[str, Any]]:
    """Bank ledgers that may be this account's, from every company with retrieved masters: the ledger
    chosen for it on the Generate page, then ledgers whose bank details carry the account number."""
    out: list[dict[str, Any]] = []
    digits = re.sub(r"\D", "", account_number)
    for company in session.scalars(select(TallyCompany).where(TallyCompany.last_sync_run_id.is_not(None))):
        _, masters = load_masters(session, company.guid)
        found: list[tuple[TallyLedger, str]] = []
        memory = session.scalar(select(MappingMemory).where(
            MappingMemory.company_guid == company.guid, MappingMemory.purpose == MappingPurpose.BANK,
            MappingMemory.key == digits)) if digits else None
        if memory is not None and memory.ledger_guid in masters.ledgers:
            found.append((masters.ledgers[memory.ledger_guid], "Chosen for this account on the Generate page"))
        found += [(c.ledger, c.reason) for c in matching.statement_bank(masters, account_number, ifsc)]
        seen = set()
        for ledger, reason in found:
            if ledger.guid in seen:
                continue
            seen.add(ledger.guid)
            out.append({"company_guid": company.guid, "company_name": company.name, "ledger_guid": ledger.guid,
                        "ledger_name": ledger.name, "reason": reason})
    return out


def bank_ledgers(session: Session, company_guid: str) -> list[TallyLedger]:
    try:
        _, masters = load_masters(session, company_guid)
    except VoucherError as exc:
        raise ReconciliationError(exc.code, exc.message) from None
    return sorted(matching.bank_ledgers(masters), key=lambda l: l.name.lower())


def link_ledger(session: Session, key: str, account_number: str, company_guid: str | None, ledger_guid: str | None,
                *, note: str | None, user_id: int | None, client_address: str | None) -> ReconciliationAccount:
    """Link the account to a TallyPrime bank ledger (or unlink it with ``ledger_guid=None``). Recorded in
    the audit trail; nothing is sent to TallyPrime."""
    account = session.scalar(select(ReconciliationAccount).where(ReconciliationAccount.account_key == key))
    if account is None:
        account = ReconciliationAccount(account_key=key, account_number=account_number[:32])
        session.add(account)
        session.flush()
    previous = ({"company_guid": account.company_guid, "ledger_guid": account.ledger_guid,
                 "ledger_name": account.ledger_name} if account.ledger_guid else None)
    if ledger_guid is None:
        if account.ledger_guid is None:
            raise ReconciliationError("not_linked", "This account is not linked to a ledger.")
        account.company_guid = account.ledger_guid = account.ledger_name = None
        account.linked_at = None
        account.linked_by_user_id = user_id
        kind, detail = ReconciliationEventKind.LEDGER_UNLINKED, {"previous": previous}
    else:
        if not company_guid:
            raise ReconciliationError("invalid", "Choose the company the ledger belongs to.")
        ledger = next((l for l in bank_ledgers(session, company_guid) if l.guid == ledger_guid), None)
        if ledger is None:
            raise ReconciliationError(
                "unknown_ledger", "That ledger is not a bank ledger in the retrieved masters. Retrieve the masters "
                                  "again on the TallyPrime page, or choose another ledger.")
        if previous and previous["ledger_guid"] == ledger.guid and previous["company_guid"] == company_guid:
            return account
        account.company_guid, account.ledger_guid, account.ledger_name = company_guid, ledger.guid, ledger.name
        account.linked_at = utc_now()
        account.linked_by_user_id = user_id
        kind = ReconciliationEventKind.LEDGER_LINKED
        detail = {"company_guid": company_guid, "ledger_guid": ledger.guid, "ledger_name": ledger.name,
                  "previous": previous}
    session.add(ReconciliationEvent(account_id=account.id, kind=kind, detail=detail, note=note,
                                    user_id=user_id, client_address=client_address))
    return account


def events(session: Session, key: str, limit: int = 50) -> list[ReconciliationEvent]:
    account = session.scalar(select(ReconciliationAccount).where(ReconciliationAccount.account_key == key))
    if account is None:
        return []
    return list(session.scalars(select(ReconciliationEvent).where(ReconciliationEvent.account_id == account.id)
                                .order_by(ReconciliationEvent.id.desc()).limit(limit)))


def statement_span(statements: list[StatementData]) -> tuple[date | None, date | None]:
    usable = [s for s in statements if s.usable]
    if not usable:
        return None, None
    return min(s.period_from for s in usable), max(s.period_to for s in usable)
