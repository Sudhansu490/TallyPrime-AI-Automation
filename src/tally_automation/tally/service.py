"""Connection checks and read-only retrieval of TallyPrime masters.

A retrieval ("sync") reads groups, ledgers and voucher types of one company
that is currently loaded in TallyPrime, and replaces that company's stored
snapshot in a single transaction. If anything fails, the previous snapshot
stays as it was and the run is recorded as failed with the reason.

Logs carry counts and timings only: no ledger names, GSTINs or account numbers.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from tally_automation.db.base import utc_now
from tally_automation.db.models import (
    SyncStatus, TallyCompany, TallyGroup, TallyLedger, TallySyncRun, TallyVoucherType,
)
from tally_automation.db.session import transaction
from tally_automation.extraction.parsing import Status, parse_gstin
from tally_automation.tally.client import TallyClient, TallyConfigError, TallyConnectionError
from tally_automation.tally.masters import Company, Masters, resolve_primary_groups
from tally_automation.tally.xmlio import TallyResponseError, TallyWriteBlocked

logger = logging.getLogger(__name__)

TallyError = (TallyConfigError, TallyConnectionError, TallyResponseError, TallyWriteBlocked)


class SyncError(Exception):
    def __init__(self, code: str, message: str, run_id: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.run_id = run_id


@dataclass
class ConnectionCheck:
    reachable: bool
    server_running: bool
    message: str
    milliseconds: int | None
    companies: list[Company] = field(default_factory=list)
    checked_at: datetime = field(default_factory=utc_now)


def check_connection(client: TallyClient) -> ConnectionCheck:
    """Is the server up, and which companies are loaded? Never raises for Tally problems."""
    try:
        status = client.server_status()
    except TallyError as exc:
        return ConnectionCheck(False, False, str(exc), None)
    if not status.running:
        return ConnectionCheck(True, False, f"Port answered, but not as TallyPrime: {status.message}", status.milliseconds)
    try:
        companies = client.companies()
    except TallyError as exc:
        return ConnectionCheck(True, True, f"Server is running, but listing companies failed: {exc}", status.milliseconds)
    message = status.message if companies else "TallyPrime is running, but no company is loaded."
    return ConnectionCheck(True, True, message, status.milliseconds, companies)


# One retrieval at a time: a second click must not run a parallel export.
_sync_lock = threading.Lock()


def sync_company(session_factory: sessionmaker[Session], client: TallyClient, company_name: str) -> int:
    """Retrieve and store one loaded company's masters. Returns the sync run id."""
    if not _sync_lock.acquire(blocking=False):
        raise SyncError("sync_in_progress", "A retrieval from TallyPrime is already running.")
    try:
        return _sync(session_factory, client, company_name)
    finally:
        _sync_lock.release()


def _sync(session_factory: sessionmaker[Session], client: TallyClient, company_name: str) -> int:
    with transaction(session_factory) as session:
        run = TallySyncRun(company_name=company_name, host=client.host, port=client.port, status=SyncStatus.RUNNING,
                           warnings=[])
        session.add(run)
        session.flush()
        run_id = run.id

    def fail(code: str, message: str) -> SyncError:
        with transaction(session_factory) as session:
            failed = session.get(TallySyncRun, run_id)
            assert failed is not None
            failed.status, failed.error_message, failed.finished_at = SyncStatus.FAILED, message[:2000], utc_now()
        logger.warning("tally sync failed", extra={"run_id": run_id, "code": code})
        return SyncError(code, message, run_id)

    try:
        # Tally silently returns nothing for a company that is not loaded, so check first.
        loaded = {c.name: c for c in client.companies()}
        company = loaded.get(company_name)
        if company is None:
            raise fail("company_not_loaded",
                       f"'{company_name}' is not loaded in TallyPrime. Loaded: {', '.join(loaded) or 'none'}.")
        masters, removed = client.masters(company.name)
    except SyncError:
        raise
    except TallyError as exc:
        raise fail("tally_error", str(exc)) from exc
    warnings = resolve_primary_groups(masters)

    with transaction(session_factory) as session:
        _replace_snapshot(session, run_id, company, masters)
        run = session.get(TallySyncRun, run_id)
        assert run is not None
        run.company_guid = company.guid
        run.status = SyncStatus.SUCCEEDED
        run.group_count, run.ledger_count = len(masters.groups), len(masters.ledgers)
        run.voucher_type_count, run.removed_characters = len(masters.voucher_types), removed
        run.warnings = warnings
        run.finished_at = utc_now()
    logger.info("tally sync finished", extra={
        "run_id": run_id, "groups": len(masters.groups), "ledgers": len(masters.ledgers),
        "voucher_types": len(masters.voucher_types), "removed_characters": removed, "warnings": len(warnings),
    })
    return run_id


def _replace_snapshot(session: Session, run_id: int, company: Company, masters: Masters) -> None:
    guid = company.guid
    stored = session.scalar(select(TallyCompany).where(TallyCompany.guid == guid))
    if stored is None:
        stored = TallyCompany(guid=guid, name=company.name)
        session.add(stored)
    stored.name, stored.books_from, stored.starting_from = company.name, company.books_from, company.starting_from
    stored.state, stored.country = company.state, company.country
    stored.last_sync_run_id, stored.last_synced_at = run_id, utc_now()

    for model in (TallyGroup, TallyLedger, TallyVoucherType):
        session.execute(delete(model).where(model.company_guid == guid))
    session.add_all(
        TallyGroup(
            company_guid=guid, sync_run_id=run_id, guid=g.guid, name=g.name, aliases=g.aliases, parent=g.parent,
            primary_group=g.primary_group, is_revenue=g.is_revenue, is_deemed_positive=g.is_deemed_positive,
            affects_gross_profit=g.affects_gross_profit, alter_id=g.alter_id,
        ) for g in _unique(masters.groups)
    )
    session.add_all(
        TallyLedger(
            company_guid=guid, sync_run_id=run_id, guid=l.guid, name=l.name, aliases=l.aliases, parent=l.parent,
            primary_group=l.primary_group, gstin=l.gstin,
            gstin_valid=None if l.gstin is None else parse_gstin(l.gstin).status is Status.VALID,
            gst_registration_type=l.gst_registration_type, state=l.state, country=l.country, tax_type=l.tax_type,
            gst_duty_head=l.gst_duty_head, tax_rate=l.tax_rate, bank_account_number=l.bank_account_number,
            ifsc=l.ifsc, is_bill_wise=l.is_bill_wise, alter_id=l.alter_id,
        ) for l in _unique(masters.ledgers)
    )
    session.add_all(
        TallyVoucherType(
            company_guid=guid, sync_run_id=run_id, guid=v.guid, name=v.name, aliases=v.aliases, parent=v.parent,
            numbering_method=v.numbering_method, is_active=v.is_active, alter_id=v.alter_id,
        ) for v in _unique(masters.voucher_types)
    )


def _unique(items: list) -> list:
    """Tally GUIDs are unique per company; keep the first if a response ever repeats one."""
    seen: dict[str, object] = {}
    for item in items:
        seen.setdefault(item.guid, item)
    return list(seen.values())


def recover_interrupted_syncs(session_factory: sessionmaker[Session]) -> int:
    """Mark retrievals left 'running' by a crash or shutdown as failed (at startup).

    Never stops the app from starting: an unmigrated database is reported by
    the health check instead.
    """
    try:
        with transaction(session_factory) as session:
            runs = session.scalars(select(TallySyncRun).where(TallySyncRun.status == SyncStatus.RUNNING)).all()
            for run in runs:
                run.status = SyncStatus.FAILED
                run.error_message = "Interrupted: the application stopped during the retrieval."
                run.finished_at = utc_now()
    except SQLAlchemyError:
        logger.warning("could not check for interrupted Tally retrievals (database not migrated?)")
        return 0
    return len(runs)
