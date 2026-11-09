"""Controlled import of approved vouchers into TallyPrime (Phase 8).

Vouchers are sent one at a time, from Computer A, and only when all of these hold:

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

Batches (one document's vouchers): the company check, the marker search and the
read-back are each done once for the whole batch rather than once per voucher.
The marker search covers the same period as before (books start to today), with
the document's shared marker prefix, so the protection is unchanged; only the
number of requests to TallyPrime drops.

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
from tally_automation.tally.importing import MARKER, ImportResult, TallyVoucher
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
           client_address: str | None, user_id: int | None) -> Snapshot:
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
            imported_by_user_id=user_id, started_at=utc_now(),
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


def _needle(markers: list[str]) -> str:
    """One narration search for all of these markers: the marker itself, the document's shared
    prefix ("TA-<file hash>-"), or every voucher of this application ("[TA-")."""
    if len(set(markers)) == 1:
        return markers[0]
    prefixes = {m.rsplit("-", 1)[0] + "-" for m in markers}
    return prefixes.pop() if len(prefixes) == 1 else "[TA-"


def _by_marker(vouchers: list[TallyVoucher]) -> dict[str, list[TallyVoucher]]:
    out: dict[str, list[TallyVoucher]] = {}
    for v in vouchers:
        if not v.deleted:
            for m in v.markers:
                out.setdefault(m, []).append(v)
    return out


def _lookup(client: TallyClient, company: str, markers: list[str], start: date, end: date, *,
            entries: bool) -> dict[str, list[TallyVoucher]]:
    return _by_marker(client.marked_vouchers(company, _needle(markers), start, end, entries=entries))


def _send(client: TallyClient, snap: Snapshot) -> Outcome | ImportResult:
    """Send one voucher. An ``Outcome`` when it is finished (not sent, refused, unclear); the
    TallyPrime result when it was created and still has to be read back."""
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
    return result


def _present_outcome(snap: Snapshot, existing: list[TallyVoucher], detailed: dict[str, list[TallyVoucher]] | None,
                     read_error: str | None) -> Outcome:
    if read_error is not None:
        verified, note = False, f"Could not read it back ({read_error})."
    else:
        first_date = existing[0].voucher_date or snap.voucher_date
        found = [v for v in (detailed or {}).get(snap.marker, []) if (v.voucher_date or snap.voucher_date) == first_date]
        verified, note = _verify(found[0], snap) if found else (False, "Could not read its entries back.")
    extra = f" {len(existing)} vouchers carry this marker; check TallyPrime for duplicates." if len(existing) > 1 else ""
    return Outcome(AttemptStatus.ALREADY_PRESENT,
                   f"Not sent: TallyPrime already has this voucher (found by its marker). {note}{extra}",
                   master_id=existing[0].master_id, verified=verified and len(existing) == 1)


def _created_outcome(snap: Snapshot, result: ImportResult, detailed: dict[str, list[TallyVoucher]] | None,
                     read_error: str | None) -> Outcome:
    found: list[TallyVoucher] = []
    if read_error is not None:
        verified, note = False, f"Created, but it could not be read back ({read_error})."
    else:
        found = [v for v in (detailed or {}).get(snap.marker, []) if v.voucher_date == snap.voucher_date]
        if len(found) == 1:
            verified, note = _verify(found[0], snap)
        else:
            verified, note = False, f"Read back by its marker, TallyPrime returned {len(found)} voucher(s); check it in TallyPrime."
    return Outcome(AttemptStatus.CREATED, f"Created in TallyPrime. {note}", created=result.created, errors=result.errors,
                   master_id=result.last_voucher_id or (found[0].master_id if verified else None), verified=verified,
                   excerpt=result.excerpt)


_FINAL = {
    AttemptStatus.CREATED: ImportStatus.IMPORTED, AttemptStatus.ALREADY_PRESENT: ImportStatus.IMPORTED,
    AttemptStatus.REFUSED: ImportStatus.FAILED, AttemptStatus.NOT_SENT: ImportStatus.FAILED,
    AttemptStatus.UNCERTAIN: ImportStatus.UNCERTAIN,
}


def _record(factory: sessionmaker[Session], snap: Snapshot, outcome: Outcome) -> None:
    with transaction(factory) as tx:
        attempt = tx.get(VoucherImport, snap.attempt_id)
        voucher = tx.get(GeneratedVoucher, snap.voucher_id)
        assert attempt is not None and voucher is not None
        attempt.status, attempt.message = outcome.status, outcome.message
        attempt.created_count, attempt.error_count = outcome.created, outcome.errors
        attempt.tally_master_id, attempt.verified = outcome.master_id, outcome.verified
        attempt.response_excerpt, attempt.finished_at = outcome.excerpt, utc_now()
        voucher.import_status = _FINAL[outcome.status]
        if voucher.import_status is ImportStatus.IMPORTED:
            voucher.imported_at, voucher.tally_master_id = utc_now(), outcome.master_id
    logger.info("voucher import finished", extra={
        "voucher_id": snap.voucher_id, "attempt_id": snap.attempt_id, "outcome": outcome.status.value,
        "verified": outcome.verified,
    })


@dataclass
class _Planned:
    voucher_id: int
    company: str
    marker: str
    voucher_date: date | None


def _planned(factory: sessionmaker[Session], voucher_ids: list[int], write_enabled: bool,
             results: dict[int, int | VoucherError]) -> dict[str, list[_Planned]]:
    """The importable vouchers grouped by company, in the order given. Read only: nothing is claimed
    yet, and a voucher that may not be imported is refused here, before TallyPrime is contacted."""
    out: dict[str, list[_Planned]] = {}
    with factory() as s:
        for voucher_id in voucher_ids:
            v = s.get(GeneratedVoucher, voucher_id)
            try:
                if v is None:
                    raise VoucherError("not_found", "Voucher not found.")
                check_importable(s, v, write_enabled)
            except VoucherError as exc:
                results[voucher_id] = exc
                continue
            out.setdefault(v.company_name, []).append(_Planned(voucher_id, v.company_name, v.marker, v.voucher_date))
    return out


def _import_company(factory: sessionmaker[Session], client: TallyClient, company_name: str, group: list[_Planned],
                    loaded: dict | None, loaded_error: str | None, results: dict[int, int | VoucherError], *,
                    write_enabled: bool, client_address: str | None, user_id: int | None) -> None:
    # 1. The company must be open in TallyPrime (the import names it). Asked once for all vouchers.
    failure = None
    found: dict[str, list[TallyVoucher]] = {}
    if loaded_error is not None:
        failure = f"Not sent: {loaded_error}"
    elif company_name not in (loaded or {}):
        failure = f"Not sent: the company “{company_name}” is not open in TallyPrime."
    else:
        # 2. Duplicate-posting protection, for all of the document's vouchers in ONE search.
        company = loaded[company_name]
        dates = [p.voucher_date for p in group if p.voucher_date is not None]
        markers = [p.marker for p in group if MARKER.match(p.marker or "")]
        if markers:
            start = min([company.books_from or EARLIEST, *dates])
            end = max([date.today(), *dates])
            try:
                found = _lookup(client, company_name, markers, start, end, entries=False)
            except (TallyConnectionError, TallyResponseError) as exc:
                failure = f"Not sent: could not check TallyPrime for an earlier import ({exc})."
            except Exception as exc:  # noqa: BLE001 - nothing was sent; recorded per voucher
                logger.exception("marker lookup crashed")
                failure = (f"Not sent: the check for an earlier import stopped unexpectedly ({type(exc).__name__}). "
                           "Import again.")

    # 3. Send, one voucher at a time; each attempt is committed as "sending" first.
    pending: list[Snapshot] = []
    present: list[tuple[Snapshot, list[TallyVoucher]]] = []
    created: list[tuple[Snapshot, ImportResult]] = []
    try:
        for p in group:
            try:
                snap = _claim(factory, p.voucher_id, write_enabled, client, client_address, user_id)
            except VoucherError as exc:
                results[p.voucher_id] = exc
                continue
            results[p.voucher_id] = snap.attempt_id
            if failure is not None:
                _record(factory, snap, Outcome(AttemptStatus.NOT_SENT, failure))
                continue
            pending.append(snap)
            existing = found.get(snap.marker, [])
            if existing:
                present.append((snap, existing))
                continue
            sent = _send(client, snap)
            if isinstance(sent, Outcome):
                pending.remove(snap)
                _record(factory, snap, sent)
            else:
                created.append((snap, sent))

        # 4. Read back, in ONE request, every voucher created now or found already there.
        if present or created:
            days = [s.voucher_date for s, _ in created]
            days += [(e[0].voucher_date or s.voucher_date) for s, e in present]
            detailed, read_error = None, None
            try:
                detailed = _lookup(client, company_name, [s.marker for s, _ in present + created],
                                   min(days), max(days), entries=True)
            except (TallyConnectionError, TallyResponseError) as exc:
                read_error = str(exc)
            for snap, existing in present:
                _record(factory, snap, _present_outcome(snap, existing, detailed, read_error))
                pending.remove(snap)
            for snap, result in created:
                _record(factory, snap, _created_outcome(snap, result, detailed, read_error))
                pending.remove(snap)
    except Exception as exc:  # noqa: BLE001 - recorded, never silently ignored
        logger.exception("voucher import crashed", extra={"voucher_ids": [s.voucher_id for s in pending]})
        for snap in pending:
            _record(factory, snap, Outcome(AttemptStatus.UNCERTAIN, f"The import stopped unexpectedly "
                                           f"({type(exc).__name__}). Import again: the next attempt first checks TallyPrime."))


def import_vouchers(factory: sessionmaker[Session], client: TallyClient, voucher_ids: list[int], *,
                    write_enabled: bool, client_address: str | None,
                    user_id: int | None = None) -> dict[int, int | VoucherError]:
    """Import approved vouchers (normally one document's) with as few TallyPrime requests as possible:
    one company check, one marker search, one request per voucher sent, and one read-back.

    Returns, per voucher id, its attempt id, or the ``VoucherError`` that stopped it before any attempt.
    """
    if not _lock.acquire(blocking=False):
        raise VoucherError("import_in_progress", "Another voucher is being imported; try again in a moment.")
    try:
        results: dict[int, int | VoucherError] = {}
        groups = _planned(factory, voucher_ids, write_enabled, results)
        if not groups:
            return {voucher_id: results[voucher_id] for voucher_id in voucher_ids if voucher_id in results}
        loaded, loaded_error = None, None
        try:
            loaded = {c.name: c for c in client.companies()}
        except (TallyConnectionError, TallyResponseError) as exc:
            loaded_error = str(exc)
        except Exception as exc:  # noqa: BLE001 - nothing was sent; recorded per voucher
            logger.exception("company check crashed")
            loaded_error = f"the company check stopped unexpectedly ({type(exc).__name__})"
        for company_name, group in groups.items():
            _import_company(factory, client, company_name, group, loaded, loaded_error, results,
                            write_enabled=write_enabled, client_address=client_address, user_id=user_id)
        return {voucher_id: results[voucher_id] for voucher_id in voucher_ids if voucher_id in results}
    finally:
        _lock.release()


def import_voucher(factory: sessionmaker[Session], client: TallyClient, voucher_id: int, *,
                   write_enabled: bool, client_address: str | None, user_id: int | None = None) -> int:
    """Import one approved voucher. Returns the attempt id. Raises ``VoucherError`` if it may not be imported."""
    result = import_vouchers(factory, client, [voucher_id], write_enabled=write_enabled,
                             client_address=client_address, user_id=user_id)[voucher_id]
    if isinstance(result, VoucherError):
        raise result
    return result


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
