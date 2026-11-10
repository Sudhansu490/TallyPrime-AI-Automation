"""Rule-based ledger matching for bank transactions. SYNTHETIC masters only."""

from __future__ import annotations

from datetime import date

from tally_automation.db.models import TallyGroup, TallyLedger
from tally_automation.vouchers.compat import Masters
from tally_automation.vouchers.matching import (
    category, counter_ledgers, name_tokens, new_party_name, party_in_narration, party_segments, statement_bank,
)


def _ledger(name: str, parent: str, **kw) -> TallyLedger:
    return TallyLedger(company_guid="c", sync_run_id=1, guid=f"l-{name}", name=name, aliases=[], parent=parent, **kw)


GROUPS = [TallyGroup(company_guid="c", sync_run_id=1, guid=n, name=n, aliases=[], parent=p) for n, p in [
    ("Sundry Creditors", "Current Liabilities"), ("Sundry Debtors", "Current Assets"),
    ("Bank Accounts", "Current Assets"), ("Cash-in-Hand", "Current Assets"),
    ("Current Liabilities", None), ("Current Assets", None), ("Indirect Expenses", None),
    ("Duties & Taxes", "Current Liabilities"), ("Loans & Advances (Asset)", "Current Assets"),
    ("Loans (Liability)", None),
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
    _ledger("P Q & R Electricals", "Sundry Creditors"),
    _ledger("Synthetic Electricals", "Sundry Debtors"),
    _ledger("Gridpower Company", "Sundry Debtors"),
    _ledger("XYZ AND QQ", "Sundry Debtors"),
    _ledger("Zeta Electric & Hardware Mart", "Sundry Creditors"),
    _ledger("Zeta Traders", "Sundry Creditors"),
    _ledger("Omega Power Corporation", "Sundry Creditors"),
    _ledger("Housing Loan Interest", "Indirect Expenses"),
    _ledger("Synthetic Housing Loan", "Loans (Liability)"),
    _ledger("Korvandell Synthetic", "Sundry Creditors"),
    _ledger("Staff Salary", "Indirect Expenses"),
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
    # TDS on cash withdrawals is a bank charge, not GST TDS; other TDS stays TDS.
    assert category("TDS CASHWD: M310542:06-11-2026:1") == "charges"
    assert category("TDS CASH WD 06-11-2026") == "charges"
    assert category("TDS/CASH WDL/SYNB12") == "charges"
    assert category("TDS DEDUCTED U/S 194A") == "tds"
    assert category("TO SELF") == "cash"
    assert category("ATM WDL SYNTHETIC") == "cash"
    assert category("INT.PD 01-04-2026") == "interest"
    assert category("NEFT_OUT:SYNB1/SYNTHETIC PAYEE KUMAR/SYNB0000001") is None


def test_counter_ledger_rules() -> None:
    m = masters()
    bank = {"l-Synthetic Main Bank"}
    assert names(counter_ledgers(m, "NEFT_CHRG:SYNB12345/SYNB0000001/12", True, bank))[:2] == ["Bank Charges", "Courier Charges"]
    for narration in ("TDS CASHWD: M310542:06-11-2026:1", "TDS CASH WD 06-11-2026"):
        assert names(counter_ledgers(m, narration, True, bank))[0] == "Bank Charges"
    tds = counter_ledgers(m, "TDS DEDUCTED U/S 194A", True, bank)
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



def test_initials_and_short_words_are_part_of_the_name() -> None:
    assert name_tokens("K L & M Electricals") == ["klm", "electricals"]
    assert name_tokens("M/S. A.B.C. TRADERS") == ["abc", "traders"]
    m = masters()
    bank = {"l-Synthetic Main Bank"}
    # A shared common word ("ELECTRICALS") is not enough: the initials must be there too.
    out = counter_ledgers(m, "NEFT_OUT:SYNB1/SYNTHETIC ELECTRICALS /SYNB0000001/12", True, bank)
    assert names(out) == ["Synthetic Electricals"]
    assert names(counter_ledgers(m, "NEFT_OUT:SYNB1/P Q AND R ELECTRICALS/SYNB0000001", True, bank))[0] ==         "P Q & R Electricals"
    # Initials plus a two-letter word identify the party; a cut-short word never stands in for a one-word name.
    payer = counter_ledgers(m, "NRTGS/SYNB52026/XYZ AND QQ POWER CORP", False, bank)
    assert names(payer) == ["XYZ AND QQ"]
    assert party_in_narration(m, "NRTGS/SYNB52026/XYZ AND QQ POWER CORP", False, bank).ledger.name == "XYZ AND QQ"
    assert party_in_narration(m, "NEFT_IN:SYNB123//SYNTHETIC POWER CORPORA", False, bank).ledger.name == \
        "Synthetic Power Corporation"



def test_a_ledger_named_by_most_of_its_name_is_found() -> None:
    m = masters()
    bank = {"l-Synthetic Main Bank"}
    # The narration gives the first words of the ledger's longer name ("ELECTRICALS" for "Electric").
    out = counter_ledgers(m, "NEFT_OUT:SYNB2/ZETA ELECTRICALS /SYNB0000001/12", True, bank)
    assert names(out)[0] == "Zeta Electric & Hardware Mart" and out[0].reason.startswith("Part of the party name")
    assert "Zeta Traders" not in names(out)                    # one shared word is not enough
    # Common words alone ("power", "corporation") never identify a party.
    assert "Omega Power Corporation" not in names(counter_ledgers(m, "NRTGS/SYNB5/XYZ AND QQ POWER CORPORATION", False, bank))


def test_transfer_to_a_loan_account_is_the_housing_loan() -> None:
    m = masters()
    bank = {"l-Synthetic Main Bank"}
    for narration in ("D10000001CR TO999999SY00000001", "D10000002 CR TO 999999SY00000002", "d10000003cr to999999sy00000003"):
        assert category(narration) == "housing_loan", narration
        found = counter_ledgers(m, narration, True, bank)
        # The loan itself first (Loans (Liability)), the interest ledger after it.
        assert names(found)[:2] == ["Synthetic Housing Loan", "Housing Loan Interest"], names(found)
    # An ordinary transfer with only digits is not a loan-account transfer.
    assert category("NEFT_OUT:SYNB1/SYNTHETIC PAYEE KUMAR/123456789012") is None


def test_a_close_spelling_of_the_payee_finds_the_ledger() -> None:
    m = masters()
    bank = {"l-Synthetic Main Bank"}
    narration = "NEFT_OUT:SYNB00000000001/KORVNDELL SYNTHETIC /SYNB0001234/1234567890"
    assert party_segments(narration) == ["KORVNDELL SYNTHETIC"]
    found = counter_ledgers(m, narration, True, bank)
    assert names(found)[0] == "Korvandell Synthetic" and found[0].reason.startswith("Similar spelling")
    # A name nothing like any ledger gets no existing ledger, but its payee name for a new one.
    other = "NEFT_OUT:SYNB00000000002/QUENTIN ZYLOPHONE /SYNB0001234/1234567890"
    assert counter_ledgers(m, other, True, bank) == []
    assert new_party_name(other) == "QUENTIN ZYLOPHONE"
    # Not a transfer narration: no party name is guessed from it.
    assert new_party_name("CASH HAND CHARGE3 - 04-11-2026") is None
    assert category("SALARY") == "salary" and names(counter_ledgers(m, "SALARY", True, bank))[0] == "Staff Salary"
