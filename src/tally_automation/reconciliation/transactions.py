"""Transaction reconciliation of one account for a period: both sides, the matches, and the bank
reconciliation statement.

A comparison is stored as it was made (``reconciliation_runs.result``); a person's approvals and rejections
are kept apart, by match signature, and applied whenever the comparison is shown (``apply_decisions``). So a
stored comparison never changes, and a decision carries over to a later comparison of the same entries.

Bank reconciliation statement (all amounts from the bank account's side, money in positive)::

    expected bank closing = TallyPrime closing
                          + (bank opening − TallyPrime opening)        # difference brought forward
                          + transactions only in the statement
                          − entries only in TallyPrime (incl. duplicates)
                          + differences inside approved / proposed matches
    unexplained difference = actual bank closing − expected bank closing

Only transactions dated inside the period are compared; the difference brought forward covers earlier ones.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from tally_automation.reconciliation.balances import Figure, Timeline, balance_at_end_of, balance_at_start_of
from tally_automation.reconciliation.matching import (
    BANK_ONLY, DUPLICATE, MATCHED, PARTIAL, TALLY_ONLY, UNCERTAIN, BankItem, TallyItem,
)
from tally_automation.reconciliation.periods import day_before
from tally_automation.tally.importing import TallyVoucher
from tally_automation.validation.accounting import money

ZERO = Decimal("0")
MAX_DAYS = 366


def bank_items(timeline: Timeline, start: date, end: date, excluded: dict[int, str]
               ) -> tuple[list[BankItem], list[dict[str, Any]], list[dict[str, Any]]]:
    """The statement transactions dated in [start, end] (each day from one statement only, as in the
    balance views), the ones left out as confirmed duplicates, and the ones that cannot be compared."""
    items, left_out, unreadable = [], [], []
    for piece in timeline.pieces:
        a, b = max(piece.start, start), min(piece.end, end)
        if a > b:
            continue
        stmt = piece.statement
        for row in stmt.rows:
            if row.txn_date is None:
                # It may belong to this period: it cannot be compared, so say so.
                unreadable.append({"document_id": stmt.document_id, "row": row.number, "date": None,
                                   "description": row.description, "reason": "No usable date."})
                continue
            if not a <= row.txn_date <= b:
                continue
            base = {"document_id": stmt.document_id, "row": row.number, "date": row.txn_date.isoformat(),
                    "description": row.description}
            if row.transaction_id in excluded:
                left_out.append({**base, "reason": excluded[row.transaction_id]})
                continue
            debit, credit = row.debit or ZERO, row.credit or ZERO
            if not row.amount_known or (debit and credit) or not (debit or credit):
                unreadable.append({**base, "reason": "No single usable debit or credit amount."})
                continue
            items.append(BankItem(row.transaction_id, stmt.document_id, row.number, row.txn_date, bool(debit),
                                  debit or credit, row.description, row.reference))
    return items, left_out, unreadable


def tally_items(vouchers: list[TallyVoucher], ledger_name: str) -> list[TallyItem]:
    """Entries on the bank ledger. TallyPrime signs debits negative: money into the bank debits it."""
    wanted = " ".join(ledger_name.lower().split())
    out = []
    for v in vouchers:
        if v.deleted or v.voucher_date is None:
            continue
        for index, entry in enumerate(v.entries):
            if " ".join(entry.ledger.lower().split()) != wanted or entry.amount != entry.amount or entry.amount == 0:
                continue
            out.append(TallyItem(f"{v.guid}#{index}", v.guid, v.voucher_date, v.voucher_type, v.number,
                                 money_out=entry.amount > 0, amount=abs(entry.amount), narration=v.narration,
                                 reference=v.reference, by_this_app=bool(v.markers)))
    return out


def bank_balance(timeline: Timeline, when: date, *, start_of_day: bool) -> Figure:
    """The account's balance at the start or end of ``when``, from the statement covering that day."""
    for piece in timeline.pieces:
        if piece.start <= when <= piece.end:
            return balance_at_start_of(piece.statement, when) if start_of_day else balance_at_end_of(piece.statement, when)
    if start_of_day:
        before = day_before(when)
        for piece in timeline.pieces:
            if piece.start <= before <= piece.end:
                return balance_at_end_of(piece.statement, before)
    return Figure(None, None, None, f"No statement covers {when:%d %b %Y}.")


def _d(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def apply_decisions(result: dict[str, Any], decisions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """A stored comparison with the current decisions applied: match statuses, the leftover lists, counts
    and the bank reconciliation statement."""
    matches, bank_only, tally_only = [], list(result["bank_only"]), list(result["tally_only"])
    for m in result["matches"]:
        decision = decisions.get(m["signature"])
        shown = {**m, "decision": decision}
        if decision and decision["decision"] == "rejected":
            bank_only += [{**b, "from_rejected_match": True} for b in m["bank"]]
            tally_only += [{**t, "from_rejected_match": True} for t in m["tally"]]
        matches.append(shown)
    active = [m for m in matches if not (m["decision"] and m["decision"]["decision"] == "rejected")]
    counts = {
        "matched": sum(1 for m in active if m["status"] == MATCHED),
        "approved": sum(1 for m in active if m["needs_approval"] and m["decision"]),
        "awaiting_approval": sum(1 for m in active if m["needs_approval"] and not m["decision"]),
        "uncertain": sum(1 for m in active if m["status"] == UNCERTAIN),
        "partial": sum(1 for m in active if m["status"] == PARTIAL),
        "rejected": len(matches) - len(active),
        "duplicate": sum(1 for m in active if m["status"] == DUPLICATE),
        "bank_only": len(bank_only), "tally_only": len(tally_only),
        "excluded": len(result["excluded"]), "unreadable": len(result["unreadable"]),
    }

    bank_only_total = sum((Decimal(b["signed"]) for b in bank_only), ZERO)
    tally_only_total = sum((Decimal(t["signed"]) for t in tally_only), ZERO)
    tally_only_total += sum((Decimal(t["signed"]) for m in active if m["status"] == DUPLICATE for t in m["tally"]), ZERO)
    differences = sum((Decimal(m["difference"]) for m in active if m["status"] != DUPLICATE), ZERO)
    balances = result["balances"]
    bank_open, bank_close = _d(balances["bank_opening"]["value"]), _d(balances["bank_closing"]["value"])
    tally_open, tally_close = _d(balances["tally_opening"]), _d(balances["tally_closing"])
    expected = unexplained = brought_forward = None
    if None not in (bank_open, tally_open):
        brought_forward = bank_open - tally_open
    if None not in (brought_forward, tally_close):
        expected = tally_close + brought_forward + bank_only_total - tally_only_total + differences
        if bank_close is not None:
            unexplained = bank_close - expected
    statement = {
        "tally_opening": money(tally_open), "bank_opening": money(bank_open), "brought_forward": money(brought_forward),
        "tally_closing": money(tally_close), "bank_only_total": money(bank_only_total),
        "tally_only_total": money(tally_only_total), "match_differences": money(differences),
        "expected_bank_closing": money(expected), "bank_closing": money(bank_close),
        "unexplained": money(unexplained),
        "status": ("unknown" if unexplained is None else MATCHED if unexplained == 0 else "mismatch"),
        "awaiting_approval": counts["awaiting_approval"],
    }
    return {**result, "matches": matches, "bank_only": bank_only, "tally_only": tally_only,
            "counts": counts, "statement": statement}


STATUS_LABELS = {MATCHED: "Matched", UNCERTAIN: "Uncertain", PARTIAL: "Partly matched", DUPLICATE: "Duplicate in TallyPrime",
                 BANK_ONLY: "Only in the statement", TALLY_ONLY: "Only in TallyPrime"}
