"""Rule-based ledger matching for bank transactions (no AI).

Each function returns candidate ledgers with the reason they were picked, best
first. The Generate page pre-fills the first candidate; the reviewer checks and
changes it before submitting, so a candidate is never used on its own.

Rules, in order of strength:

* the statement's bank account: the full account number, or (when the bank
  prints it masked, e.g. XXXXXXXX0074) the visible last digits, preferring a
  ledger with the same IFSC;
* a transfer to another of the company's own bank accounts: an account number
  in the narration equal to a bank ledger's account number;
* a narration category: bank charges, TDS deducted by the bank, cash
  withdrawal / deposit, interest;
* a party name: every word of a ledger's name appears in the narration (the
  last word may be cut short, as banks truncate narrations), preferring
  creditors for payments and debtors for receipts.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from tally_automation.db.models import TallyLedger
from tally_automation.vouchers.compat import Masters

BANK_ACCOUNT_GROUPS = {"Bank Accounts", "Bank OD A/c", "Bank OCC A/c"}
ASSET_GROUPS = {"Current Assets", "Loans & Advances (Asset)"}

# Words that say nothing about the counter-party.
_NOISE = {
    "neft", "rtgs", "nrtgs", "imps", "upi", "out", "in", "inw", "outw", "chrg", "trf", "transfer", "to", "by",
    "from", "the", "and", "of", "for", "ltd", "limited", "pvt", "private", "co", "company", "cr", "dr",
    "ac", "acc", "account", "bank", "mr", "mrs", "ms", "shri", "smt", "m", "s",
}

CATEGORY_LABELS = {
    "charges": "bank charges",
    "tds": "TDS deducted by the bank",
    "cash": "cash withdrawal or deposit",
    "interest": "interest",
}


@dataclass(frozen=True)
class Candidate:
    ledger: TallyLedger
    reason: str


def words(text: str | None) -> list[str]:
    return re.findall(r"[a-z]+", (text or "").lower().replace("&", " and "))


def digits(text: str | None) -> str:
    return re.sub(r"\D", "", text or "")


def category(description: str | None) -> str | None:
    """What kind of entry a narration describes, from its wording alone."""
    text = (description or "").upper()
    if re.search(r"\bTDS\b", text):
        return "tds"
    if re.search(r"CHRG|CHARGE|CHGS|\bCHG\b|COMMISSION|\bFEE\b|\bFEES\b|SMS ALERT|\bGST ON\b", text):
        return "charges"
    if re.search(r"\bINT(?:EREST)?\b|INT\.?PD|INT\.?COLL", text):
        return "interest"
    if re.search(r"\bSELF\b|CASHWD|CASH WDL|CASH WITHDRAW|\bATM\b|\bCWDR\b|CASH DEP|BY CASH|\bCASH\b", text):
        return "cash"
    return None


def _unique(items: Iterable[Candidate]) -> list[Candidate]:
    seen: set[str] = set()
    out: list[Candidate] = []
    for item in items:
        if item.ledger.guid not in seen:
            seen.add(item.ledger.guid)
            out.append(item)
    return out


def bank_ledgers(masters: Masters) -> list[TallyLedger]:
    return [l for l in masters.ledgers.values() if masters.in_groups(l, BANK_ACCOUNT_GROUPS)]


def statement_bank(masters: Masters, account_number: str | None, ifsc: str | None) -> list[Candidate]:
    """Bank ledgers for the statement's own account."""
    shown = digits(account_number)
    if len(shown) < 4:
        return []
    wanted_ifsc = (ifsc or "").strip().upper()
    full: list[Candidate] = []
    tail: list[tuple[int, Candidate]] = []
    for ledger in bank_ledgers(masters):
        number = digits(ledger.bank_account_number)
        same_ifsc = bool(wanted_ifsc) and (ledger.ifsc or "").strip().upper() == wanted_ifsc
        if number and number == shown:
            full.append(Candidate(ledger, "Same account number as the statement"))
        elif number and number.endswith(shown[-4:]):
            reason = "Account number ends with the statement's last digits" + (" and same IFSC" if same_ifsc else "")
            tail.append((0 if same_ifsc else 1, Candidate(ledger, reason)))
        elif shown[-4:] in digits(ledger.name):
            tail.append((2, Candidate(ledger, "Ledger name contains the statement's last account digits")))
    return _unique(full + [c for _, c in sorted(tail, key=lambda t: (t[0], t[1].ledger.name))])


def _own_account(masters: Masters, description: str, bank_guids: set[str]) -> list[Candidate]:
    numbers = set(re.findall(r"\d{9,18}", description))
    if not numbers:
        return []
    return [Candidate(l, "Transfer to the company's own bank account in the narration")
            for l in bank_ledgers(masters)
            if l.guid not in bank_guids and digits(l.bank_account_number) in numbers]


def _by_category(masters: Masters, kind: str) -> list[Candidate]:
    ledgers = sorted(masters.ledgers.values(), key=lambda l: l.name.lower())
    named = lambda l, *parts: all(p in l.name.lower() for p in parts)
    if kind == "charges":
        best = [Candidate(l, "Bank charges ledger") for l in ledgers if named(l, "bank", "charg")]
        other = [Candidate(l, "Charges ledger") for l in ledgers
                 if named(l, "charg") and masters.in_groups(l, {"Indirect Expenses"})]
        return _unique(best + other)
    if kind == "tds":
        tds = [l for l in ledgers if "tds" in words(l.name) or named(l, "tax deducted")]
        cash_tds = [Candidate(l, "TDS on cash withdrawal ledger") for l in tds
                    if "cash" in l.name.lower() or "194n" in l.name.lower()]
        assets = [Candidate(l, "TDS ledger (asset side)") for l in tds if masters.in_groups(l, ASSET_GROUPS)]
        return _unique(cash_tds + assets + [Candidate(l, "TDS ledger") for l in tds])
    if kind == "cash":
        in_hand = [Candidate(l, "Cash ledger") for l in ledgers if masters.in_groups(l, {"Cash-in-Hand"})]
        named_cash = [Candidate(l, "Ledger named cash") for l in ledgers
                      if "cash" in words(l.name) and masters.in_groups(l, {"Current Assets"})]
        return _unique(in_hand + named_cash)
    if kind == "interest":
        return [Candidate(l, "Interest ledger") for l in ledgers if "interest" in l.name.lower()]
    return []


def _name_words(name: str) -> set[str]:
    return {w for w in words(name) if len(w) >= 3 and w not in _NOISE}


def _by_name(masters: Masters, description: str, money_out: bool | None) -> list[Candidate]:
    said = {w for w in words(description) if w not in _NOISE}
    if not said:
        return []
    found: list[tuple[int, int, Candidate]] = []
    for ledger in masters.ledgers.values():
        if masters.is_duty(ledger) or masters.in_groups(ledger, BANK_ACCOUNT_GROUPS):
            continue
        for name in [ledger.name, *(ledger.aliases or [])]:
            needed = _name_words(name)
            if len(needed) < 2 and not (len(needed) == 1 and len(next(iter(needed), "")) >= 6):
                continue  # one short word ("ram", "ali") matches far too much
            missing = needed - said
            # Banks cut narrations short: the one missing word may appear truncated.
            if len(missing) > 1 or (missing and not any(
                    len(w) >= 4 and next(iter(missing)).startswith(w) for w in said)):
                continue
            preferred = {"Sundry Creditors"} if money_out else {"Sundry Debtors"} if money_out is False else set()
            rank = 0 if preferred and masters.in_groups(ledger, preferred) else 1
            reason = "Party name in the narration" + (" (last word cut short)" if missing else "")
            found.append((rank, -len(needed), Candidate(ledger, reason)))
            break
    found.sort(key=lambda t: (t[0], t[1], t[2].ledger.name.lower()))
    return _unique(c for _, _, c in found)


def counter_ledgers(masters: Masters, description: str | None, money_out: bool | None,
                    bank_guids: set[str]) -> list[Candidate]:
    """Candidate 'other' ledgers for one bank transaction."""
    text = description or ""
    out = _own_account(masters, text, bank_guids)
    kind = category(text)
    if kind is not None:
        out += _by_category(masters, kind)
    out += _by_name(masters, text, money_out)
    return [c for c in _unique(out) if c.ledger.guid not in bank_guids]
