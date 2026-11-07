"""Human approval of generated vouchers (Phase 8).

An approval is tied to the exact XML (its SHA-256): a voucher generated again
is a different voucher and needs its own approval. Only a *ready* voucher that
is not out of date can be approved, and its warnings must be acknowledged.
Every decision is appended to ``voucher_approvals``; nothing is ever deleted.

Until login exists (Phase 9) the API accepts decisions only from Computer A
itself; the deciding device is recorded with every decision.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from tally_automation.db.base import utc_now
from tally_automation.db.models import (
    ApprovalDecision, ApprovalStatus, GeneratedVoucher, ImportStatus, VoucherApproval, VoucherStatus,
)
from tally_automation.vouchers.service import VoucherError, stale_reasons

LOCKED_IMPORT_STATES = {ImportStatus.IMPORTING, ImportStatus.IMPORTED, ImportStatus.UNCERTAIN}


def _voucher(session: Session, voucher_id: int) -> GeneratedVoucher:
    voucher = session.get(GeneratedVoucher, voucher_id)
    if voucher is None:
        raise VoucherError("not_found", "Voucher not found.")
    if voucher.status is VoucherStatus.SUPERSEDED:
        raise VoucherError("superseded", "This voucher was replaced by a later generation; decide on the current one.")
    return voucher


def _locked(voucher: GeneratedVoucher) -> None:
    if voucher.import_status in LOCKED_IMPORT_STATES:
        state = {ImportStatus.IMPORTING: "is being imported", ImportStatus.IMPORTED: "is already in TallyPrime",
                 ImportStatus.UNCERTAIN: "may already be in TallyPrime (the last import had no clear answer)"}
        raise VoucherError("import_locked", f"This voucher {state[voucher.import_status]}; its approval cannot change.")


def _record(session: Session, voucher: GeneratedVoucher, decision: ApprovalDecision, *, note: str | None,
            client_address: str | None, warnings: int = 0) -> VoucherApproval:
    record = VoucherApproval(
        voucher_id=voucher.id, decision=decision, xml_sha256=voucher.xml_sha256, warnings_acknowledged=warnings,
        note=note, client_address=client_address, decided_at=utc_now(),
    )
    session.add(record)
    return record


def approve(session: Session, voucher_id: int, *, acknowledge_warnings: bool, note: str | None,
            client_address: str | None) -> GeneratedVoucher:
    voucher = _voucher(session, voucher_id)
    _locked(voucher)
    if voucher.status is not VoucherStatus.READY or voucher.xml_sha256 is None:
        raise VoucherError("blocked", "Only a ready voucher can be approved; this one has errors.")
    if voucher.approval_status is ApprovalStatus.APPROVED:
        raise VoucherError("already_approved", "This voucher is already approved.")
    reasons = stale_reasons(session, voucher)
    if reasons:
        raise VoucherError("stale", "The voucher is out of date: " + " ".join(reasons))
    if voucher.warning_count and not acknowledge_warnings:
        raise VoucherError("warnings_not_acknowledged",
                           f"Read and acknowledge the voucher's {voucher.warning_count} warning(s) before approving.")
    voucher.approval_status = ApprovalStatus.APPROVED
    voucher.approved_xml_sha256 = voucher.xml_sha256
    _record(session, voucher, ApprovalDecision.APPROVED, note=note, client_address=client_address,
            warnings=voucher.warning_count)
    return voucher


def reject(session: Session, voucher_id: int, *, note: str, client_address: str | None) -> GeneratedVoucher:
    voucher = _voucher(session, voucher_id)
    _locked(voucher)
    if voucher.approval_status is ApprovalStatus.REJECTED:
        raise VoucherError("already_rejected", "This voucher is already rejected.")
    voucher.approval_status = ApprovalStatus.REJECTED
    voucher.approved_xml_sha256 = None
    if voucher.import_status is ImportStatus.FAILED:
        voucher.import_status = ImportStatus.NOT_IMPORTED
    _record(session, voucher, ApprovalDecision.REJECTED, note=note, client_address=client_address)
    return voucher


def revoke(session: Session, voucher_id: int, *, note: str | None, client_address: str | None,
           voucher: GeneratedVoucher | None = None) -> GeneratedVoucher:
    voucher = voucher or _voucher(session, voucher_id)
    _locked(voucher)
    if voucher.approval_status is not ApprovalStatus.APPROVED:
        raise VoucherError("not_approved", "This voucher is not approved.")
    voucher.approval_status = ApprovalStatus.PENDING
    voucher.approved_xml_sha256 = None
    if voucher.import_status is ImportStatus.FAILED:
        voucher.import_status = ImportStatus.NOT_IMPORTED
    _record(session, voucher, ApprovalDecision.REVOKED, note=note, client_address=client_address)
    return voucher
