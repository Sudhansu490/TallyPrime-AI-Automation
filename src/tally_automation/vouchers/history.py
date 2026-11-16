"""Which ledger each party has been booked to in TallyPrime before.

The vouchers of the past year are read from TallyPrime (read only). For every bank voucher whose narration
names a party the way banks print it ("NEFT-<ref>-<NAME>", "NEFT_OUT:<ref>/<NAME>/…"), the one other
ledger of the voucher is the ledger that party was booked to. Per party, the ledger used most often (the
most recent on a tie) is stored in the mapping memory; the Generate page suggests it first for that party.

Reading a year takes tens of seconds, so it runs after "Retrieve masters" (in the background) or on demand,
never while the Generate page loads. Nothing is written to TallyPrime.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import date, timedelta

from sqlalchemy import delete
from sqlalchemy.orm import Session, sessionmaker

from tally_automation.db.models import MappingMemory, MappingPurpose
from tally_automation.db.session import transaction
from tally_automation.tally.client import TallyClient
from tally_automation.vouchers.matching import bank_ledgers, party_key
from tally_automation.vouchers.service import TALLY_PARTY_PREFIX, VoucherError, load_masters, tally_party_key

logger = logging.getLogger(__name__)

LEARN_DAYS = 365
CHUNK_DAYS = 31


def learn_from_tally(factory: sessionmaker[Session], client: TallyClient, company_guid: str, *,
                     days: int = LEARN_DAYS, today: date | None = None) -> tuple[int, int]:
    """Read the past ``days`` of vouchers and store party → ledger. Returns (vouchers read, parties learned)."""
    today = today or date.today()
    with factory() as session:
        company, masters = load_masters(session, company_guid)
        company_name = company.name
    by_name = {l.name.strip().lower(): l for l in masters.ledgers.values()}
    bank_names = {l.name.strip().lower() for l in bank_ledgers(masters)}
    # TallyPrime only answers for an open company; asking for another one just runs into the timeout.
    if company_name not in {c.name for c in client.companies()}:
        raise VoucherError("company_not_open", f"“{company_name}” is not open in TallyPrime. Open it there and try again.")
    # A month per request: a busy company's whole year can take longer than one request may wait.
    vouchers = []
    start = today - timedelta(days=days)
    while start <= today:
        end = min(start + timedelta(days=CHUNK_DAYS - 1), today)
        vouchers += client.period_vouchers(company_name, start, end)
        start = end + timedelta(days=1)

    counts: dict[str, Counter] = {}
    latest: dict[tuple[str, str], date] = {}
    for voucher in vouchers:
        if voucher.deleted:
            continue
        party = party_key(voucher.narration)
        if party is None:
            continue
        names = [e.ledger.strip().lower() for e in voucher.entries if e.ledger]
        banks = [n for n in names if n in bank_names]
        others = {n for n in names if n not in bank_names}
        if len(banks) != 1 or len(others) != 1:
            continue                  # not a plain bank voucher with one other ledger
        ledger = by_name.get(others.pop())
        if ledger is None:
            continue
        counts.setdefault(party, Counter())[ledger.guid] += 1
        when = voucher.voucher_date or date.min
        latest[(party, ledger.guid)] = max(latest.get((party, ledger.guid), date.min), when)

    with transaction(factory) as tx:
        tx.execute(delete(MappingMemory).where(
            MappingMemory.company_guid == company_guid, MappingMemory.purpose == MappingPurpose.COUNTER,
            MappingMemory.key.like(TALLY_PARTY_PREFIX + "%")))
        for party, ledgers in counts.items():
            best = max(ledgers, key=lambda guid: (ledgers[guid], latest[(party, guid)]))
            tx.add(MappingMemory(company_guid=company_guid, purpose=MappingPurpose.COUNTER, key=tally_party_key(party),
                                 ledger_guid=best, name=masters.ledgers[best].name, use_count=ledgers[best]))
    logger.info("party ledgers learned from TallyPrime", extra={"vouchers": len(vouchers), "parties": len(counts)})
    return len(vouchers), len(counts)
