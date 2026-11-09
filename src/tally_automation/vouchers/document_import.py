"""One-click approval and import of a whole document's vouchers.

The approver's single click on "Approve & import into TallyPrime" is the
explicit approval: every current, ready voucher of the document that is not yet
in TallyPrime is approved (recording the user, the device and the warnings they
were shown) and then imported, one at a time, through the same guarded
importer as before. That importer still looks for each voucher's marker in
TallyPrime first, so clicking again after a failure or an unclear answer never
posts a voucher twice.

Blocked, rejected and out-of-date vouchers are never approved or imported;
they are reported back as skipped with the reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session, sessionmaker

from tally_automation.db.models import ApprovalStatus, AttemptStatus, ImportStatus, VoucherImport, VoucherStatus
from tally_automation.db.session import transaction
from tally_automation.tally.client import TallyClient
from tally_automation.vouchers import approval
from tally_automation.vouchers.importer import import_voucher
from tally_automation.vouchers.service import VoucherError, document_vouchers

@dataclass
class ItemResult:
    voucher_id: int
    outcome: str            # imported | already_present | failed | skipped
    message: str


@dataclass
class DocumentResult:
    items: list[ItemResult] = field(default_factory=list)

    def count(self, outcome: str) -> int:
        return sum(1 for i in self.items if i.outcome == outcome)


def _approve_all(factory: sessionmaker[Session], document_id: int, result: DocumentResult, *,
                 client_address: str | None, user_id: int | None, separate_approver: bool) -> list[int]:
    """Approve what can be approved; return the ids to import, in document order."""
    to_import: list[int] = []
    with transaction(factory) as tx:
        vouchers = document_vouchers(tx, document_id)
        if not vouchers:
            raise VoucherError("no_vouchers", "This document has no generated vouchers yet. Generate them first.")
        for v in vouchers:
            if v.import_status is ImportStatus.IMPORTED:
                continue
            if v.import_status is ImportStatus.IMPORTING:
                result.items.append(ItemResult(v.id, "skipped", "It is being imported right now."))
                continue
            if v.status is VoucherStatus.BLOCKED:
                result.items.append(ItemResult(v.id, "skipped", "It has errors; fix them and generate it again."))
                continue
            if v.approval_status is ApprovalStatus.REJECTED:
                result.items.append(ItemResult(v.id, "skipped", "It was rejected."))
                continue
            if v.approval_status is not ApprovalStatus.APPROVED:
                try:   # approve() checks everything before it changes anything
                    approval.approve(tx, v.id, acknowledge_warnings=True, note="Approved and imported in one step",
                                     client_address=client_address, user_id=user_id,
                                     separate_approver=separate_approver)
                except VoucherError as exc:
                    result.items.append(ItemResult(v.id, "skipped", exc.message))
                    continue
            to_import.append(v.id)
    return to_import


def approve_and_import_document(factory: sessionmaker[Session], client: TallyClient, document_id: int, *,
                                write_enabled: bool, client_address: str | None, user_id: int | None,
                                separate_approver: bool) -> DocumentResult:
    if not write_enabled:
        raise VoucherError("write_disabled", "Writing to TallyPrime is disabled. Set TALLY_WRITE_ENABLED=true in .env "
                                             "on Computer A and restart the application.")
    result = DocumentResult()
    to_import = _approve_all(factory, document_id, result, client_address=client_address, user_id=user_id,
                             separate_approver=separate_approver)
    for voucher_id in to_import:
        try:
            attempt_id = import_voucher(factory, client, voucher_id, write_enabled=write_enabled,
                                        client_address=client_address, user_id=user_id)
        except VoucherError as exc:
            result.items.append(ItemResult(voucher_id, "failed", exc.message))
            continue
        with factory() as s:
            attempt = s.get(VoucherImport, attempt_id)
            assert attempt is not None
            status, message = attempt.status, attempt.message or ""
        if status is AttemptStatus.CREATED:
            outcome = "imported"
        elif status is AttemptStatus.ALREADY_PRESENT:
            outcome = "already_present"
        else:
            outcome = "failed"
        result.items.append(ItemResult(voucher_id, outcome, message))
    return result
