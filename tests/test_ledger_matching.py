"""Rule-based ledger matching for bank transactions. SYNTHETIC masters only."""

from __future__ import annotations

from datetime import date

from tally_automation.db.models import TallyGroup, TallyLedger
from tally_automation.vouchers.compat import Masters
from tally_automation.vouchers.matching import category, counter_ledgers, statement_bank


def _ledger(name: str, parent: str, **kw) -> TallyLedger:
    return TallyLedger(company_guid="c", sync_run_id=1, guid=f"l-{name}", name=name, aliases=[], parent=parent, **kw)


GROUPS = [TallyGroup(company_guid="c", sync_run_id=1, guid=n, name=n, aliases=[], parent=p) for n, p in [
    ("Sundry Creditors", "Current Liabilities"), ("Sundry Debtors", "Current Assets"),
    ("Bank Accounts", "Current Assets"), ("Cash-in-Hand", "Current Assets"),
    ("Current Liabilities", None), ("Current Assets", None), ("Indirect Expenses", None),
    ("Duties & Taxes", "Current Liabilities"), ("Loans & Advances (Asset)", "Current Assets"),
]]
LEDGERS = [
    _ledger("Synthetic Main Bank", "Bank Accounts", bank_account_number="111122223333", ifsc="SYNB0000001"),
    _ledger("Synthetic Old Bank", "Bank Accounts", bank_account_number="999922223333", ifsc="OTHR0000009"),
    _ledger("Synthetic Second Bank", "Bank Accounts", bank_account_number="444455556666"),
    _ledger("Cash", "Cash-in-Hand"),
    _ledger("Bank Charges", "Indirect Expenses"),
    _ledger("Courier Charges", "Indirect Expenses"),
    _ledger("TDS Receivable", "Loans & Advances (Asset)"),
    _ledger("TDS Payable 194C", "Duties & Taxes"),
    _ledger("Synthetic Power Corporation", "Sundry Debtors"),
    _ledger("Synthetic Payee Kumar", "Sundry Creditors"),
    _ledger("Ram", "Sundry Creditors"),
    _ledger("Interest Received", "Indirect Expenses"),
]


def masters() -> Masters:
    return Masters("c", "SYNTHETIC CO", "Maharashtra", date(2025, 4, 1), {l.guid: l for l in LEDGERS},
                   {g.name: g for g in GROUPS}, {})


def names(candidates) -> list[str]:
    return [c.ledger.name for c in candidates]


def test_masked_account_prefers_same_ifsc() -> None:
    found = statement_bank(masters(), "XXXXXXXX3333", "SYNB0000001")
    assert names(found) == ["Synthetic Main Bank", "Synthetic Old Bank"]
    assert "same IFSC" in found[0].reason
    assert names(statement_bank(masters(), "111122223333", None))[0] == "Synthetic Main Bank"
    assert statement_bank(masters(), None, None) == []


def test_categories_from_the_narration() -> None:
    assert category("NEFT_CHRG:SYNB12345/SYNB0000001/12") == "charges"
    assert category("TDS CASHWD: M310542:06-11-2026:1") == "tds"
    assert category("TO SELF") == "cash"
    assert category("ATM WDL SYNTHETIC") == "cash"
    assert category("INT.PD 01-04-2026") == "interest"
    assert category("NEFT_OUT:SYNB1/SYNTHETIC PAYEE KUMAR/SYNB0000001") is None


def test_counter_ledger_rules() -> None:
    m = masters()
    bank = {"l-Synthetic Main Bank"}
    assert names(counter_ledgers(m, "NEFT_CHRG:SYNB12345/SYNB0000001/12", True, bank))[:2] == ["Bank Charges", "Courier Charges"]
    tds = counter_ledgers(m, "TDS CASHWD: M310542:06-11-2026:1", True, bank)
    assert names(tds)[0] == "TDS Receivable" and "asset" in tds[0].reason
    assert names(counter_ledgers(m, "TO SELF", True, bank)) == ["Cash"]
    # Party name, with the narration's last word cut short by the bank.
    power = counter_ledgers(m, "NEFT_IN:SYNB123//SYNTHETIC POWER CORPORA", False, bank)
    assert names(power) == ["Synthetic Power Corporation"] and "cut short" in power[0].reason
    payee = counter_ledgers(m, "NEFT_OUT:SYNB1/SYNTHETIC PAYEE KUMAR/SYNB0000001", True, bank)
    assert names(payee) == ["Synthetic Payee Kumar"]
    # Transfer to another of the company's own bank accounts.
    own = counter_ledgers(m, "NEFT_OUT:SYNB2/SYNTHETIC CO/444455556666", True, bank)
    assert names(own)[0] == "Synthetic Second Bank"


def test_weak_matches_are_not_suggested() -> None:
    m = masters()
    bank = {"l-Synthetic Main Bank"}
    # A one-word short ledger name ("Ram") would match far too many narrations.
    assert names(counter_ledgers(m, "NEFT_OUT:SYNB1/RAM TRADERS/SYNB0000001", True, bank)) == []
    # The statement's own bank is never offered as the other ledger.
    assert "Synthetic Main Bank" not in names(counter_ledgers(m, "NEFT_OUT:X/111122223333", True, bank))
    # Duties ledgers are never matched by name.
    assert "TDS Payable 194C" not in names(counter_ledgers(m, "NEFT_OUT:X/TDS PAYABLE SYNTHETIC", True, bank)[:1])
