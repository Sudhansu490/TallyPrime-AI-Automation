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
* a party name: every distinctive word of a ledger's name appears in the
  narration. Initials count ("K L & M" and "KLM" are the same word) and so do
  two-letter words ("DD"), so a ledger is never matched on one shared common
  word. In a name of two or more words, one word may be cut short, since banks
  truncate narrations. The match covering the most of the name comes first;
  between equal matches, creditors are preferred for payments and debtors for
  receipts.
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
    "housing_loan": "housing loan",
}


@dataclass(frozen=True)
class Candidate:
    ledger: TallyLedger
    reason: str


def words(text: str | None) -> list[str]:
    return re.findall(r"[a-z]+", (text or "").lower().replace("&", " and "))


def digits(text: str | None) -> str:
    return re.sub(r"\D", "", text or "")


_TDS_CASH_WITHDRAWAL = re.compile(r"\bTDS\b[\s:/_.-]*CASH[\s_.-]*(?:WD|WDL|WITHDRAWALS?)\b")


def tds_on_cash_withdrawal(description: str | None) -> bool:
    """"TDS CASH WD" / "TDS CASHWD": the bank's deduction on cash withdrawals. It is booked as a
    bank charge, never as GST TDS or another TDS ledger."""
    return bool(_TDS_CASH_WITHDRAWAL.search((description or "").upper()))


# "D16578842CR TO117310NC00000073": a transfer to a loan account (branch digits, a letter scheme code,
# digits), which the user always books to the Housing Loan ledger.
_HOUSING_LOAN_TRANSFER = re.compile(r"\bD\d{5,}\s*CR\b[\s/:_-]*TO\s*\d{3,}[A-Z]{1,4}\d{4,}\b")


def housing_loan_transfer(description: str | None) -> bool:
    return bool(_HOUSING_LOAN_TRANSFER.search((description or "").upper()))


def fixed_category(description: str | None) -> str | None:
    """Narrations the user has said are ALWAYS one kind of entry, whatever was chosen before."""
    if tds_on_cash_withdrawal(description):
        return "charges"
    if housing_loan_transfer(description):
        return "housing_loan"
    return None


def category(description: str | None) -> str | None:
    """What kind of entry a narration describes, from its wording alone."""
    text = (description or "").upper()
    fixed = fixed_category(text)
    if fixed is not None:
        return fixed
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
    if kind == "housing_loan":
        loans = [l for l in ledgers if "housing loan" in " ".join(words(l.name)) or "home loan" in " ".join(words(l.name))]
        # The loan itself (under Loans (Liability)) first; an interest ledger last.
        loans.sort(key=lambda l: (not masters.in_groups(l, {"Loans (Liability)"}), "interest" in words(l.name),
                                  l.name.lower()))
        return [Candidate(l, "Housing loan ledger") for l in loans]
    return []


PARTY_NAME = "Party name in the narration"
MIN_NAME_LETTERS = 5


_INITIALS_GAP = re.compile(r"[\s.&]*|[\s.]*and[\s.]*")


def name_tokens(text: str | None) -> list[str]:
    """Words of a name or narration with runs of initials joined: "K L & M", "K.L.M." and "K L AND M"
    all give "klm". Initials separated by anything else ("M/S. A.B.C.") stay apart."""
    lowered = re.sub(r"\bm\s*/\s*s\b\.?", " ", (text or "").lower())   # "M/S." (messrs) is not a name
    found = [(m.group(), m.start(), m.end()) for m in re.finditer(r"[a-z]+", lowered)]
    out: list[str] = []
    run, run_end = "", 0
    i = 0
    while i < len(found):
        word, start, end = found[i]
        if word == "and" and run and i + 1 < len(found) and len(found[i + 1][0]) == 1                 and _INITIALS_GAP.fullmatch(lowered[run_end:found[i + 1][1]]):
            i += 1
            continue
        if len(word) == 1:
            if run and not _INITIALS_GAP.fullmatch(lowered[run_end:start]):
                if len(run) > 1:
                    out.append(run)
                run = ""
            run, run_end = run + word, end
        else:
            if len(run) > 1:
                out.append(run)
            run = ""
            out.append(word)
        i += 1
    if len(run) > 1:
        out.append(run)
    return out


def _name_words(name: str) -> list[str]:
    return list(dict.fromkeys(w for w in name_tokens(name) if w not in _NOISE))


FULL, CUT, PART = 0, 1, 2
_REASONS = {FULL: PARTY_NAME, CUT: PARTY_NAME + " (a word cut short)", PART: "Part of the party name in the narration"}


def _word_in(word: str, said: set[str]) -> str | None:
    """'exact', 'prefix' (one is the start of the other: banks truncate, names abbreviate) or None."""
    if word in said:
        return "exact"
    if len(word) >= 4 and any(len(w) >= 4 and (w.startswith(word) or word.startswith(w)) for w in said):
        return "prefix"
    return None


def _name_match(needed: list[str], said: set[str]) -> tuple[int, int] | None:
    """(tier, letters matched) when the narration names this ledger, else None.

    FULL: every word of the name is in the narration. CUT: the same, but one word only as a
    shortened form. PART: at least two words and at least half of the name, starting with its
    first word, which is what tells parties apart ("KLM ...", "Zeta ..."), unlike common words
    such as "power" or "corporation".
    """
    if not needed or sum(len(w) for w in needed) < MIN_NAME_LETTERS:
        return None   # "Ram" or "AB" would match far too many narrations
    if len(needed) == 1:
        return (FULL, len(needed[0])) if len(needed[0]) >= 6 and needed[0] in said else None
    found = {w: _word_in(w, said) for w in needed}
    hits = [w for w, how in found.items() if how]
    exact = [w for w, how in found.items() if how == "exact"]
    letters = sum(len(w) for w in hits)
    if len(hits) == len(needed) and len(exact) >= len(needed) - 1:
        return (FULL if len(exact) == len(needed) else CUT), letters
    if (len(hits) >= 2 and found[needed[0]] == "exact" and 2 * len(hits) >= len(needed)
            and letters >= MIN_NAME_LETTERS):
        return PART, letters
    return None


def _by_name(masters: Masters, description: str, money_out: bool | None) -> list[Candidate]:
    said = {w for w in name_tokens(description) if w not in _NOISE}
    if not said:
        return []
    preferred = {"Sundry Creditors"} if money_out else {"Sundry Debtors"} if money_out is False else set()
    found: list[tuple[int, int, int, Candidate]] = []
    for ledger in masters.ledgers.values():
        if masters.is_duty(ledger) or masters.in_groups(ledger, BANK_ACCOUNT_GROUPS):
            continue
        best: tuple[int, int] | None = None
        for name in [ledger.name, *(ledger.aliases or [])]:
            match = _name_match(_name_words(name), said)
            if match is not None and (best is None or (match[0], -match[1]) < (best[0], -best[1])):
                best = match
        if best is None:
            continue
        tier, letters = best
        rank = 0 if preferred and masters.in_groups(ledger, preferred) else 1
        found.append((tier, -letters, rank, Candidate(ledger, _REASONS[tier])))
    # The fuller the match the better; between equal matches, the expected group first.
    found.sort(key=lambda t: (t[0] == PART, t[1], t[0], t[2], t[3].ledger.name.lower()))
    return _unique(c for *_, c in found)


def party_in_narration(masters: Masters, description: str | None, money_out: bool | None,
                       bank_guids: set[str]) -> Candidate | None:
    """The ledger the narration names best, if any (its whole name, or most of it)."""
    for c in _by_name(masters, description or "", money_out):
        if c.ledger.guid not in bank_guids:
            return c
    return None


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
