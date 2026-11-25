"""Release vouchers that were imported but have since been deleted in TallyPrime.

A voucher imported into TallyPrime is locked: it is never generated or imported again, so nothing is
posted twice. If the entries are then deleted in TallyPrime (imported into the wrong company, say),
"Check TallyPrime again" looks each such voucher up by its marker in the company it was imported into
(read only). A voucher TallyPrime no longer has is released, with the reason recorded, so the document
can be generated again; one it still has stays locked. Nothing is written to TallyPrime.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session, sessionmaker

from tally_automation.db.base import utc_now
from tally_automation.db.models import (
    ApprovalDecision, ApprovalStatus, GeneratedVoucher, ImportStatus, VoucherApproval,
)
from tally_automation.db.session import transaction
from tally_automation.tally.client import TallyClient
from tally_automation.vouchers.importer import EARLIEST, _lookup
from tally_automation.vouchers.service import VoucherError, document_vouchers

RECHECKED = (ImportStatus.IMPORTED, ImportStatus.UNCERTAIN)


@dataclass
class RecheckResult:
    checked: int
    released: int
    still_in_tally: int


def release_deleted(factory: sessionmaker[Session], client: TallyClient, document_id: int, *,
                    client_address: str | None, user_id: int | None) -> RecheckResult:
    with factory() as session:
        found = [(v.id, v.company_name, v.marker, v.voucher_date) for v in document_vouchers(session, document_id)
                 if v.import_status in RECHECKED]
    if not found:
        return RecheckResult(0, 0, 0)
    loaded = {c.name: c for c in client.companies()}
    present: set[int] = set()
    for company_name in sorted({company for _, company, _, _ in found}):
        company = loaded.get(company_name)
        if company is None:
            raise VoucherError("company_not_open", f"These vouchers were imported into “{company_name}”, which is not "
                                                   "open in TallyPrime. Open it there and check again.")
        group = [f for f in found if f[1] == company_name]
        dates = [d for *_, d in group if d is not None]
        start = min([company.books_from or EARLIEST, *dates])
        end = max([date.today(), *dates])
        in_tally = _lookup(client, company_name, [m for _, _, m, _ in group], start, end, entries=False)
        present |= {voucher_id for voucher_id, _, marker, _ in group if in_tally.get(marker)}

    released = 0
    with transaction(factory) as tx:
        for voucher_id, *_ in found:
            if voucher_id in present:
                continue
            voucher = tx.get(GeneratedVoucher, voucher_id)
            if voucher is None or voucher.import_status not in RECHECKED:
                continue
            voucher.import_status = ImportStatus.NOT_IMPORTED
            voucher.imported_at, voucher.tally_master_id = None, None
            voucher.approval_status, voucher.approved_xml_sha256 = ApprovalStatus.PENDING, None
            tx.add(VoucherApproval(
                voucher_id=voucher.id, decision=ApprovalDecision.REVOKED, xml_sha256=voucher.xml_sha256,
                warnings_acknowledged=0, decided_at=utc_now(), client_address=client_address,
                decided_by_user_id=user_id,
                note="Not found in TallyPrime any more (deleted there): released so it can be generated again.",
            ))
            released += 1
    return RecheckResult(len(found), released, len(present))
