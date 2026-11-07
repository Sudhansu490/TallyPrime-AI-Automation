"""Controlled import of approved vouchers into TallyPrime (Phase 8).

One voucher at a time, from Computer A, and only when all of these hold:

* ``TALLY_WRITE_ENABLED=true`` (otherwise the client cannot write at all);
* the voucher is ready, approved for exactly its current XML (SHA-256), not out
  of date, and not already imported;
* the target company is loaded in TallyPrime.

Duplicate-posting protection: before sending, TallyPrime is searched (read-only)
for a voucher whose narration carries this voucher's marker. If one exists,
nothing is sent and the voucher is recorded as already present. Because every
attempt starts with this lookup, retrying after an unclear answer (timeout,
lost connection) can never post twice.

After TallyPrime reports the voucher created, it is read back by its marker and
its date, type and ledger entries are compared with what was approved.

States: the attempt is first committed as ``sending`` (and the voucher as
``importing``), so a crash in the middle is visible; at the next start such
attempts become ``uncertain``.
"""

from __future__ import annotations

import logging
import threading
from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from tally_automation.db.base import utc_now
from tally_automation.db.models import (
    ApprovalStatus, AttemptStatus, GeneratedVoucher, ImportStatus, VoucherImport, VoucherStatus,
)
from tally_automation.db.session import transaction
from tally_automation.tally.client import TallyClient, TallyConnectionError, TallyNotSentError
from tally_automation.tally.importing import TallyVoucher
from tally_automation.tally.xmlio import TallyResponseError, TallyWriteBlocked
from tally_automation.vouchers.service import VoucherError, stale_reasons

logger = logging.getLogger(__name__)
_lock = threading.Lock()
IMPORTABLE = {ImportStatus.NOT_IMPORTED, ImportStatus.FAILED, ImportStatus.UNCERTAIN}
EARLIEST = date(2000, 1, 1)


@dataclass
class Snapshot:
    """What is needed from the voucher outside the database session."""

    voucher_id: int
    attempt_id: int
    company: str
    marker: str
    xml: str
    voucher_date: date
    voucher_type: str
    entries: Counter


@dataclass
class Outcome:
    status: AttemptStatus
    message: str
    created: int | None = None
    errors: int | None = None
    master_id: str | None = None
    verified: bool | None = None
    excerpt: str | None = None


def _expected(entries: list[dict]) -> Counter:
    """(ledger, TallyPrime-signed amount) pairs: debit negative, credit positive."""
    out: Counter = Counter()
    for e in entries:
        amount = Decimal(e["debit"]) * -1 if e["debit"] else Decimal(e["credit"])
        out[(e["ledger"], amount)] += 1
    return out


def check_importable(session: Session, voucher: GeneratedVoucher, write_enabled: bool) -> None:
    if not write_enabled:
        raise VoucherError("write_disabled", "Writing to TallyPrime is disabled. Set TALLY_WRITE_ENABLED=true in .env "
                                             "on Computer A and restart the application to allow imports.")
    if voucher.status is not VoucherStatus.READY or voucher.xml is None:
        raise VoucherError("not_ready", "Only a ready (and approved) voucher can be imported.")
    if voucher.import_status is ImportStatus.IMPORTED:
        raise VoucherError("already_imported", "This voucher is already in TallyPrime.")
    if voucher.import_status not in IMPORTABLE:
        raise VoucherError("import_in_progress", "This voucher is being imported right now.")
    if voucher.approval_status is not ApprovalStatus.APPROVED or voucher.approved_xml_sha256 != voucher.xml_sha256:
        raise VoucherError("not_approved", "The voucher must be approved before it is imported.")
    reasons = stale_reasons(session, voucher)
    if reasons:
        raise VoucherError("stale", "The voucher is out of date: " + " ".join(reasons))


def _claim(factory: sessionmaker[Session], voucher_id: int, write_enabled: bool, client: TallyClient,
           client_address: str | None) -> Snapshot:
    with transaction(factory) as tx:
        voucher = tx.get(GeneratedVoucher, voucher_id)
        if voucher is None:
            raise VoucherError("not_found", "Voucher not found.")
        check_importable(tx, voucher, write_enabled)
        # Conditional update: only one request can move the voucher into "importing".
        claimed = tx.execute(
            update(GeneratedVoucher)
            .where(GeneratedVoucher.id == voucher_id, GeneratedVoucher.import_status.in_(IMPORTABLE))
            .values(import_status=ImportStatus.IMPORTING)
        ).rowcount
        if claimed != 1:
            raise VoucherError("import_in_progress", "This voucher is being imported right now.")
        attempt_no = (tx.scalar(select(func.max(VoucherImport.attempt)).where(VoucherImport.voucher_id == voucher_id)) or 0) + 1
        attempt = VoucherImport(
            voucher_id=voucher_id, attempt=attempt_no, status=AttemptStatus.SENDING, company_name=voucher.company_name,
            host=client.host, port=client.port, xml_sha256=voucher.xml_sha256, client_address=client_address,
            started_at=utc_now(),
        )
        tx.add(attempt)
        tx.flush()
        assert voucher.voucher_date is not None and voucher.voucher_type_name is not None
        return Snapshot(voucher_id, attempt.id, voucher.company_name, voucher.marker, voucher.xml,
                        voucher.voucher_date, voucher.voucher_type_name, _expected(voucher.entries))


def _verify(found: TallyVoucher, snap: Snapshot) -> tuple[bool, str]:
    problems = []
    if found.voucher_date != snap.voucher_date:
        problems.append("date")
    if found.voucher_type != snap.voucher_type:
        problems.append("voucher type")
    if Counter((e.ledger, e.amount) for e in found.entries) != snap.entries:
        problems.append("ledger entries")
    if problems:
        return False, "Read back from TallyPrime, the voucher differs in: " + ", ".join(problems) + ". Check it in TallyPrime."
    return True, "Read back from TallyPrime: date, voucher type and every ledger entry match the approved voucher."


def _find(client: TallyClient, snap: Snapshot, start: date, end: date, *, entries: bool) -> list[TallyVoucher]:
    vouchers = client.marked_vouchers(snap.company, snap.marker, start, end, entries=entries)
    return [v for v in vouchers if snap.marker in v.markers and not v.deleted]


def _talk(client: TallyClient, snap: Snapshot) -> Outcome:
    # 1. The company must be open in TallyPrime (the import names it).
    try:
        loaded = {c.name: c for c in client.companies()}
    except (TallyConnectionError, TallyResponseError) as exc:
        return Outcome(AttemptStatus.NOT_SENT, f"Not sent: {exc}")
    company = loaded.get(snap.company)
    if company is None:
        return Outcome(AttemptStatus.NOT_SENT, f"Not sent: the company “{snap.company}” is not open in TallyPrime.")

    # 2. Duplicate-posting protection: is a voucher with this marker already there?
    start = min(company.books_from or EARLIEST, snap.voucher_date)
    end = max(date.today(), snap.voucher_date)
    try:
        existing = _find(client, snap, start, end, entries=False)
    except (TallyConnectionError, TallyResponseError) as exc:
        return Outcome(AttemptStatus.NOT_SENT, f"Not sent: could not check TallyPrime for an earlier import ({exc}).")
    if existing:
        try:
            detailed = _find(client, snap, existing[0].voucher_date or snap.voucher_date,
                             existing[0].voucher_date or snap.voucher_date, entries=True)
            verified, note = _verify(detailed[0], snap) if detailed else (False, "Could not read its entries back.")
        except (TallyConnectionError, TallyResponseError) as exc:
            verified, note = False, f"Could not read it back ({exc})."
        extra = f" {len(existing)} vouchers carry this marker; check TallyPrime for duplicates." if len(existing) > 1 else ""
        return Outcome(AttemptStatus.ALREADY_PRESENT,
                       f"Not sent: TallyPrime already has this voucher (found by its marker). {note}{extra}",
                       master_id=existing[0].master_id, verified=verified and len(existing) == 1)

    # 3. Send.
    try:
        result = client.import_voucher(snap.xml)
    except TallyWriteBlocked as exc:
        return Outcome(AttemptStatus.NOT_SENT, f"Not sent: {exc}")
    except TallyNotSentError as exc:
        return Outcome(AttemptStatus.NOT_SENT, f"Not sent: {exc}")
    except (TallyConnectionError, TallyResponseError) as exc:
        return Outcome(AttemptStatus.UNCERTAIN,
                       f"Sent, but TallyPrime's answer was missing or unclear ({exc}). Import again: the next attempt "
                       "first checks whether TallyPrime created it.")
    if not result.succeeded:
        if result.created == 0 and (result.errors or result.messages):
            return Outcome(AttemptStatus.REFUSED, "TallyPrime refused the voucher: " +
                           ("; ".join(result.messages) or f"{result.errors} error(s)."),
                           created=result.created, errors=result.errors, excerpt=result.excerpt)
        return Outcome(AttemptStatus.UNCERTAIN,
                       f"TallyPrime's answer was unexpected (created {result.created}, altered {result.altered}, "
                       f"errors {result.errors}). Check TallyPrime before importing again.",
                       created=result.created, errors=result.errors, master_id=result.last_voucher_id, excerpt=result.excerpt)

    # 4. Read it back.
    try:
        found = _find(client, snap, snap.voucher_date, snap.voucher_date, entries=True)
        if len(found) == 1:
            verified, note = _verify(found[0], snap)
        else:
            verified, note = False, f"Read back by its marker, TallyPrime returned {len(found)} voucher(s); check it in TallyPrime."
    except (TallyConnectionError, TallyResponseError) as exc:
        verified, note = False, f"Created, but it could not be read back ({exc})."
    return Outcome(AttemptStatus.CREATED, f"Created in TallyPrime. {note}", created=result.created, errors=result.errors,
                   master_id=result.last_voucher_id or (found[0].master_id if verified else None), verified=verified,
                   excerpt=result.excerpt)


_FINAL = {
    AttemptStatus.CREATED: ImportStatus.IMPORTED, AttemptStatus.ALREADY_PRESENT: ImportStatus.IMPORTED,
    AttemptStatus.REFUSED: ImportStatus.FAILED, AttemptStatus.NOT_SENT: ImportStatus.FAILED,
    AttemptStatus.UNCERTAIN: ImportStatus.UNCERTAIN,
}


def import_voucher(factory: sessionmaker[Session], client: TallyClient, voucher_id: int, *,
                   write_enabled: bool, client_address: str | None) -> int:
    """Import one approved voucher. Returns the attempt id. Raises ``VoucherError`` if it may not be imported."""
    if not _lock.acquire(blocking=False):
        raise VoucherError("import_in_progress", "Another voucher is being imported; try again in a moment.")
    try:
        snap = _claim(factory, voucher_id, write_enabled, client, client_address)
        try:
            outcome = _talk(client, snap)
        except Exception as exc:  # noqa: BLE001 - recorded, never silently ignored
            logger.exception("voucher import crashed", extra={"voucher_id": voucher_id})
            outcome = Outcome(AttemptStatus.UNCERTAIN, f"The import stopped unexpectedly ({type(exc).__name__}). "
                                                       "Import again: the next attempt first checks TallyPrime.")
        with transaction(factory) as tx:
            attempt = tx.get(VoucherImport, snap.attempt_id)
            voucher = tx.get(GeneratedVoucher, voucher_id)
            assert attempt is not None and voucher is not None
            attempt.status, attempt.message = outcome.status, outcome.message
            attempt.created_count, attempt.error_count = outcome.created, outcome.errors
            attempt.tally_master_id, attempt.verified = outcome.master_id, outcome.verified
            attempt.response_excerpt, attempt.finished_at = outcome.excerpt, utc_now()
            voucher.import_status = _FINAL[outcome.status]
            if voucher.import_status is ImportStatus.IMPORTED:
                voucher.imported_at, voucher.tally_master_id = utc_now(), outcome.master_id
        logger.info("voucher import finished", extra={
            "voucher_id": voucher_id, "attempt_id": snap.attempt_id, "outcome": outcome.status.value,
            "verified": outcome.verified,
        })
        return snap.attempt_id
    finally:
        _lock.release()


def recover_interrupted_imports(factory: sessionmaker[Session]) -> int:
    """Attempts left 'sending' by a crash may or may not have reached TallyPrime: mark them uncertain."""
    try:
        with transaction(factory) as tx:
            attempts = tx.scalars(select(VoucherImport).where(VoucherImport.status == AttemptStatus.SENDING)).all()
            for attempt in attempts:
                attempt.status, attempt.finished_at = AttemptStatus.UNCERTAIN, utc_now()
                attempt.message = "The application stopped during this import. Import again: the next attempt first checks TallyPrime."
            tx.execute(update(GeneratedVoucher).where(GeneratedVoucher.import_status == ImportStatus.IMPORTING)
                       .values(import_status=ImportStatus.UNCERTAIN))
            return len(attempts)
    except SQLAlchemyError:
        return 0   # database not migrated yet; nothing to recover
