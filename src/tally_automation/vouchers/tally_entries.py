"""Bank statement transactions that are already entered in TallyPrime.

Before vouchers are generated, the vouchers of the statement's period are read from TallyPrime (read
only) and paired with the statement's transactions: same date, same amount, same direction on the
chosen bank ledger. Each TallyPrime voucher pairs with at most one transaction, in statement order, so
five identical ₹5.61 charges on a day are only "already entered" as often as TallyPrime has them.

The Generate page shows these transactions as already entered and leaves them unticked, so they are
not posted twice. Nothing is written to TallyPrime here.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from tally_automation.tally.client import TallyClient
from tally_automation.tally.importing import TallyVoucher
from tally_automation.vouchers.drafts import plan_transaction
from tally_automation.vouchers.service import VoucherError, _source, load_masters


@dataclass
class EnteredVoucher:
    transaction_id: int
    voucher_type: str | None
    voucher_date: date | None
    number: str | None
    amount: Decimal
    by_this_app: bool            # carries one of this application's markers


def _key(voucher_date: date, amount: Decimal, money_out: bool) -> tuple[date, Decimal, bool]:
    return voucher_date, abs(amount).quantize(Decimal("0.01")), money_out


def find_entered(session: Session, client: TallyClient, document_id: int, company_guid: str,
                 bank_guid: str) -> list[EnteredVoucher]:
    """The statement's transactions that TallyPrime already has a voucher for on the bank ledger."""
    source = _source(session, document_id)
    if source.statement is None:
        raise VoucherError("wrong_type", "This document is not a bank statement.")
    company, masters = load_masters(session, company_guid)
    bank = masters.ledgers.get(bank_guid)
    if bank is None:
        raise VoucherError("unknown_ledger", "The bank ledger is not in the retrieved masters.")
    plans = [plan_transaction(t.id, t.sequence, t.fields)
             for t in sorted(source.statement.transactions, key=lambda t: t.sequence)]
    plans = [p for p in plans if p.txn_date is not None and p.amount is not None and p.money_out is not None]
    if not plans:
        return []
    vouchers = client.period_vouchers(company.name, min(p.txn_date for p in plans), max(p.txn_date for p in plans))

    # Each TallyPrime voucher's entries on the bank ledger. TallyPrime signs debits negative: money into
    # the bank debits the bank ledger, money out credits it.
    pool: dict[tuple[date, Decimal, bool], list[TallyVoucher]] = defaultdict(list)
    wanted = bank.name.strip().lower()
    for voucher in vouchers:
        if voucher.deleted or voucher.voucher_date is None:
            continue
        for entry in voucher.entries:
            if entry.ledger.strip().lower() == wanted and entry.amount == entry.amount and entry.amount != 0:
                pool[_key(voucher.voucher_date, entry.amount, money_out=entry.amount > 0)].append(voucher)

    found = []
    for plan in plans:
        matches = pool.get(_key(plan.txn_date, plan.amount, plan.money_out))
        if not matches:
            continue
        voucher = matches.pop(0)
        found.append(EnteredVoucher(plan.transaction_id, voucher.voucher_type, voucher.voucher_date, voucher.number,
                                    abs(plan.amount), by_this_app=bool(voucher.markers)))
    return found
