"""Bank statement transactions against the TallyPrime bank ledger (pure logic).

Each statement transaction is paired with an entry on the bank ledger in TallyPrime. In order:

1. **Exact** – same date, same amount, same direction. Matched without asking.
2. **Reference** – the statement's reference / UTR / cheque number appears in the TallyPrime voucher, the
   direction agrees and the dates are at most 31 days apart. Same amount: *uncertain* (dates differ);
   different amount: *partial*. Both need a person's approval.
3. **Date window** – same amount and direction, dates up to ``window_days`` apart: *uncertain*, needs
   approval. The nearest date is proposed; other candidates are mentioned.
4. **Split** – one entry equals the total of 2–4 entries on the other side on the same day: *partial*,
   needs approval.

What is left over is *only in the statement* or *only in TallyPrime*. A TallyPrime entry left over with the
same date, amount and direction as an exact match, when the statement has no further such transaction, is a
*duplicate* in TallyPrime (entered twice). Identical transactions on the statement are never treated as
duplicates of each other: banks really do charge the same fee several times a day.

Amounts are signed from the bank account's side: money in is positive, money out negative.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from itertools import combinations
from typing import Any

from tally_automation.validation.accounting import money

ZERO = Decimal("0")
REFERENCE_DAYS = 31
MAX_SPLIT_PARTS = 4
MAX_SPLIT_CANDIDATES = 12

MATCHED = "matched"           # exact: no approval needed
UNCERTAIN = "uncertain"       # same amount, but dates differ or several candidates: approve or reject
PARTIAL = "partial"           # amounts differ, or one-to-many: approve or reject
DUPLICATE = "duplicate"       # TallyPrime has the entry more often than the statement
BANK_ONLY = "bank_only"
TALLY_ONLY = "tally_only"

_TOKEN = re.compile(r"[A-Z0-9]{6,}")


@dataclass(frozen=True)
class BankItem:
    transaction_id: int
    document_id: int
    number: int                  # row on the statement
    txn_date: date
    money_out: bool
    amount: Decimal              # positive
    description: str | None
    reference: str | None

    @property
    def signed(self) -> Decimal:
        return -self.amount if self.money_out else self.amount

    def tokens(self) -> set[str]:
        found = set()
        for value in (self.reference, self.description):
            found |= {t for t in _TOKEN.findall((value or "").upper()) if any(c.isdigit() for c in t)}
        return found

    def to_json(self) -> dict[str, Any]:
        return {"transaction_id": self.transaction_id, "document_id": self.document_id, "row": self.number,
                "date": self.txn_date.isoformat(), "money_out": self.money_out, "amount": money(self.amount),
                "signed": money(self.signed), "description": self.description, "reference": self.reference}


@dataclass(frozen=True)
class TallyItem:
    key: str                     # "<voucher GUID>#<entry index>"
    guid: str
    voucher_date: date
    voucher_type: str | None
    number: str | None
    money_out: bool
    amount: Decimal              # positive
    narration: str | None
    reference: str | None
    by_this_app: bool

    @property
    def signed(self) -> Decimal:
        return -self.amount if self.money_out else self.amount

    def text(self) -> str:
        return " ".join(v for v in (self.narration, self.reference, self.number) if v).upper()

    def to_json(self) -> dict[str, Any]:
        return {"key": self.key, "guid": self.guid, "date": self.voucher_date.isoformat(),
                "voucher_type": self.voucher_type, "number": self.number, "money_out": self.money_out,
                "amount": money(self.amount), "signed": money(self.signed), "narration": self.narration,
                "reference": self.reference, "by_this_app": self.by_this_app}


@dataclass
class Match:
    kind: str                    # exact / reference / date / split / duplicate
    status: str
    bank: list[BankItem]
    tally: list[TallyItem]
    reason: str
    days_apart: int = 0

    @property
    def difference(self) -> Decimal:
        """Statement total − TallyPrime total (signed)."""
        return sum((b.signed for b in self.bank), ZERO) - sum((t.signed for t in self.tally), ZERO)

    @property
    def signature(self) -> str:
        """Stable id of this pairing, so a decision survives a later comparison of the same entries."""
        bank = ",".join(str(i) for i in sorted(b.transaction_id for b in self.bank))
        tally = ",".join(sorted(t.key for t in self.tally))
        return hashlib.sha256(f"b:{bank}|t:{tally}".encode()).hexdigest()[:40]

    @property
    def needs_approval(self) -> bool:
        return self.status in (UNCERTAIN, PARTIAL)

    def to_json(self) -> dict[str, Any]:
        return {"signature": self.signature, "kind": self.kind, "status": self.status, "reason": self.reason,
                "days_apart": self.days_apart, "difference": money(self.difference),
                "needs_approval": self.needs_approval,
                "bank": [b.to_json() for b in self.bank], "tally": [t.to_json() for t in self.tally]}


@dataclass
class MatchResult:
    matches: list[Match] = field(default_factory=list)       # matched / uncertain / partial / duplicate
    bank_only: list[BankItem] = field(default_factory=list)
    tally_only: list[TallyItem] = field(default_factory=list)


def _key(item_date: date, money_out: bool, amount: Decimal) -> tuple[date, bool, Decimal]:
    return item_date, money_out, amount


def _subset(items: list, target: Decimal) -> list | None:
    """The smallest group of 2–4 items whose amounts add up to ``target``."""
    if len(items) > MAX_SPLIT_CANDIDATES:
        return None
    for size in range(2, MAX_SPLIT_PARTS + 1):
        for combo in combinations(items, size):
            if sum((i.amount for i in combo), ZERO) == target:
                return list(combo)
    return None


def match(bank: list[BankItem], tally: list[TallyItem], *, window_days: int = 3) -> MatchResult:
    result = MatchResult()
    bank_left = sorted(bank, key=lambda b: (b.txn_date, b.document_id, b.number))
    tally_left = sorted(tally, key=lambda t: (t.voucher_date, t.number or "", t.key))

    # 1. Exact.
    pool: dict[tuple, list[TallyItem]] = {}
    for t in tally_left:
        pool.setdefault(_key(t.voucher_date, t.money_out, t.amount), []).append(t)
    exact_keys: set[tuple] = set()
    remaining = []
    for b in bank_left:
        candidates = pool.get(_key(b.txn_date, b.money_out, b.amount))
        if candidates:
            tokens = b.tokens()
            chosen = next((t for t in candidates if tokens and any(tok in t.text() for tok in tokens)), candidates[0])
            candidates.remove(chosen)
            exact_keys.add(_key(b.txn_date, b.money_out, b.amount))
            result.matches.append(Match("exact", MATCHED, [b], [chosen], "Same date, amount and direction."))
        else:
            remaining.append(b)
    bank_left = remaining
    tally_left = [t for group in pool.values() for t in group]

    # 2. Reference.
    remaining = []
    for b in bank_left:
        tokens = b.tokens()
        candidates = [t for t in tally_left if tokens and t.money_out == b.money_out
                      and abs((t.voucher_date - b.txn_date).days) <= REFERENCE_DAYS
                      and any(tok in t.text() for tok in tokens)]
        if not candidates:
            remaining.append(b)
            continue
        chosen = min(candidates, key=lambda t: (abs(t.amount - b.amount), abs((t.voucher_date - b.txn_date).days)))
        tally_left.remove(chosen)
        days = abs((chosen.voucher_date - b.txn_date).days)
        others = f" {len(candidates) - 1} other voucher(s) also carry it." if len(candidates) > 1 else ""
        if chosen.amount == b.amount:
            result.matches.append(Match("reference", UNCERTAIN, [b], [chosen],
                                        f"Same reference and amount, {days} day(s) apart.{others}", days))
        else:
            result.matches.append(Match("reference", PARTIAL, [b], [chosen],
                                        f"Same reference, different amount ({days} day(s) apart).{others}", days))
    bank_left = remaining

    # 3. Date window.
    remaining = []
    for b in bank_left:
        candidates = [t for t in tally_left if t.money_out == b.money_out and t.amount == b.amount
                      and abs((t.voucher_date - b.txn_date).days) <= window_days]
        if not candidates:
            remaining.append(b)
            continue
        chosen = min(candidates, key=lambda t: (abs((t.voucher_date - b.txn_date).days), t.voucher_date))
        tally_left.remove(chosen)
        days = abs((chosen.voucher_date - b.txn_date).days)
        others = f" {len(candidates) - 1} other voucher(s) would also fit." if len(candidates) > 1 else ""
        result.matches.append(Match("date", UNCERTAIN, [b], [chosen],
                                    f"Same amount and direction, {days} day(s) apart.{others}", days))
    bank_left = remaining

    # 4. Split: one TallyPrime entry for several statement transactions of the same day, and the reverse.
    for t in list(tally_left):
        group = [b for b in bank_left if b.txn_date == t.voucher_date and b.money_out == t.money_out]
        parts = _subset(group, t.amount)
        if parts:
            tally_left.remove(t)
            for p in parts:
                bank_left.remove(p)
            result.matches.append(Match("split", PARTIAL, parts, [t],
                                        f"One TallyPrime entry for {len(parts)} statement transactions of the same day."))
    for b in list(bank_left):
        group = [t for t in tally_left if t.voucher_date == b.txn_date and t.money_out == b.money_out]
        parts = _subset(group, b.amount)
        if parts:
            bank_left.remove(b)
            for p in parts:
                tally_left.remove(p)
            result.matches.append(Match("split", PARTIAL, [b], parts,
                                        f"One statement transaction entered as {len(parts)} TallyPrime entries."))

    # Duplicates in TallyPrime, then what is left on each side.
    open_bank_keys = {_key(b.txn_date, b.money_out, b.amount) for b in bank_left}
    for t in tally_left:
        k = _key(t.voucher_date, t.money_out, t.amount)
        if k in exact_keys and k not in open_bank_keys:
            result.matches.append(Match("duplicate", DUPLICATE, [], [t],
                                        "TallyPrime has this entry more times than the statement shows it."))
        else:
            result.tally_only.append(t)
    result.bank_only = bank_left
    return result
