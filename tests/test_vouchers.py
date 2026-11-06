"""Voucher drafts, import XML and TallyPrime compatibility checks. SYNTHETIC data only."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from tally_automation.db.models import TallyGroup, TallyLedger, TallyVoucherType, VoucherKind
from tally_automation.tally.xmlio import TallyWriteBlocked, assert_read_only
from tally_automation.vouchers.compat import Masters, bank_checks, purchase_checks, same_state
from tally_automation.vouchers.drafts import (
    BankLedgers, Ledger, PurchaseLedgers, bank_voucher_kind, build_bank, build_purchase, plan_invoice,
    plan_transaction,
)
from tally_automation.vouchers.xml import render_xml, verify_xml

GSTIN_MH = "27AAAAA0000A1Z2"          # synthetic, valid check character, Maharashtra
COMPANY = "SYNTHETIC TRADERS (TEST)"


def f(value: str | None, status: str = "valid") -> dict:
    return {"raw": value, "value": value, "status": status if value is not None else "missing"}


def invoice(**values: str | None) -> dict:
    base = {
        "supplier_name": "Synthetic Supplies Pvt Ltd", "supplier_gstin": GSTIN_MH, "invoice_number": "SYN-0001",
        "invoice_date": "2026-04-01", "total_taxable_value": "1000.00", "total_cgst": "90.00",
        "total_sgst": "90.00", "grand_total": "1180.00",
    }
    base.update(values)
    return {k: f(v) for k, v in base.items()}


def line(taxable: str, cgst: str | None = "9", sgst: str | None = "9", igst: str | None = None) -> dict:
    return {"taxable_amount": f(taxable), "cgst_rate": f(cgst), "sgst_rate": f(sgst), "igst_rate": f(igst)}


# ---- Synthetic masters -------------------------------------------------------------------------

def _ledger(guid: str, name: str, parent: str, **kw) -> TallyLedger:
    return TallyLedger(company_guid="c", sync_run_id=1, guid=guid, name=name, aliases=[], parent=parent, **kw)


LEDGERS = [
    _ledger("party", "Synthetic Supplies Pvt Ltd", "Sundry Creditors", gstin=GSTIN_MH, gstin_valid=True, is_bill_wise=True),
    _ledger("purchase", "Purchase @18% (Synthetic)", "Purchase Accounts"),
    _ledger("purchase5", "Purchase @5% (Synthetic)", "Purchase Accounts"),
    _ledger("cgst", "Input CGST 9%", "Duties & Taxes", gst_duty_head="CGST", tax_rate=Decimal("9"), tax_type="GST"),
    _ledger("sgst", "Input SGST 9%", "Duties & Taxes", gst_duty_head="SGST/UTGST", tax_rate=Decimal("9"), tax_type="GST"),
    _ledger("igst", "Input IGST", "Duties & Taxes", gst_duty_head="IGST", tax_rate=Decimal("0"), tax_type="GST"),
    _ledger("round", "Rounding Off", "Indirect Expenses"),
    _ledger("bank", "Synthetic Bank & Co", "Bank Accounts", bank_account_number="123456789012"),
    _ledger("cash", "Cash", "Cash-in-Hand"),
]
GROUPS = [TallyGroup(company_guid="c", sync_run_id=1, guid=n, name=n, aliases=[], parent=p) for n, p in [
    ("Current Liabilities", None), ("Sundry Creditors", "Current Liabilities"), ("Duties & Taxes", "Current Liabilities"),
    ("Current Assets", None), ("Bank Accounts", "Current Assets"), ("Cash-in-Hand", "Current Assets"),
    ("Purchase Accounts", None), ("Indirect Expenses", None),
]]
TYPES = [TallyVoucherType(company_guid="c", sync_run_id=1, guid=n, name=n, aliases=[], parent=p, is_active=a,
                          numbering_method="None") for n, p, a in [
    ("Purchase", "Purchase", True), ("Payment", "Payment", True), ("Receipt", "Receipt", True),
    ("Contra", "Contra", True), ("GST Purchase", "Purchase", True), ("Old Purchase", "Purchase", False),
]]


def masters(state: str = "Maharashtra") -> Masters:
    return Masters("c", COMPANY, state, date(2025, 4, 1), {l.guid: l for l in LEDGERS},
                   {g.name: g for g in GROUPS}, {v.name: v for v in TYPES})


def ref(m: Masters, guid: str) -> Ledger:
    l = m.ledgers[guid]
    return Ledger(l.guid, l.name, bool(l.is_bill_wise), m.is_bank_or_cash(l))


def purchase_ledgers(m: Masters, **override) -> PurchaseLedgers:
    values = dict(voucher_type="Purchase", party=ref(m, "party"), purchase={"18": ref(m, "purchase")},
                  duty={"CGST": ref(m, "cgst"), "SGST": ref(m, "sgst")}, round_off=None)
    values.update(override)
    return PurchaseLedgers(**values)


def failing(checks) -> set[str]:
    return {c.code for c in checks if not c.passed and c.severity != "info"}


# ---- Planning -------------------------------------------------------------------------------------

def test_plan_balances_and_splits_by_rate() -> None:
    plan = plan_invoice(invoice(), [line("600.00"), line("400.00")])
    assert plan.buildable and [b.key for b in plan.buckets] == ["18"] and plan.taxes == {"CGST": Decimal("90.00"), "SGST": Decimal("90.00")}

    mixed = plan_invoice(invoice(total_cgst="77.50", total_sgst="77.50", grand_total="1155.00"),
                         [line("750.00"), line("250.00", "2.5", "2.5")])
    assert [(b.key, b.taxable) for b in mixed.buckets] == [("5", Decimal("250.00")), ("18", Decimal("750.00"))]


def test_lines_that_do_not_add_up_use_one_purchase_ledger() -> None:
    plan = plan_invoice(invoice(), [line("600.00"), line("399.00")])
    assert [b.key for b in plan.buckets] == ["all"] and plan.buildable
    split = next(c for c in plan.checks if c.code == "purchase_split")
    assert split.difference == "-1.00"


def test_amounts_that_do_not_add_up_build_no_voucher() -> None:
    plan = plan_invoice(invoice(grand_total="1190.00"), [line("1000.00")])
    bad = next(c for c in plan.checks if c.code == "voucher_balances")
    assert not plan.buildable and (bad.calculated, bad.printed, bad.difference) == ("1180.00", "1190.00", "-10.00")


@pytest.mark.parametrize("missing", ["invoice_number", "invoice_date", "grand_total", "total_taxable_value"])
def test_required_values_are_never_invented(missing: str) -> None:
    plan = plan_invoice(invoice(**{missing: None}), [line("1000.00")])
    assert not plan.buildable and f"{missing}_required" in failing(plan.checks)


def test_unreadable_tax_is_an_error_not_zero() -> None:
    fields = invoice()
    fields["total_cgst"] = {"raw": "9O.00", "value": None, "status": "invalid"}
    assert "total_cgst_unreadable" in failing(plan_invoice(fields, []).checks)


# ---- Drafts and XML --------------------------------------------------------------------------------

def test_purchase_voucher_entries_and_xml() -> None:
    m = masters()
    plan = plan_invoice(invoice(total_cgst="90.25", total_sgst="90.25", round_off="-0.50", grand_total="1180.00"),
                        [line("1000.00")])
    draft = build_purchase(plan, purchase_ledgers(m, round_off=ref(m, "round")), marker="TA-TEST-INV", document_id=7)
    assert [(e.role, e.debit, str(e.amount)) for e in draft.entries] == [
        ("party", False, "1180.00"), ("purchase", True, "1000.00"), ("duty:CGST", True, "90.25"),
        ("duty:SGST", True, "90.25"), ("round_off", False, "0.50"),
    ]
    assert draft.entries[0].bill.bill_type == "New Ref" and draft.entries[0].bill.name == "SYN-0001"
    assert draft.debit_total == draft.credit_total == Decimal("1180.50")

    xml = render_xml(draft, COMPANY)
    assert verify_xml(xml, draft, COMPANY) == []
    assert '<VOUCHER VCHTYPE="Purchase" ACTION="Create"' in xml
    assert "<LEDGERNAME>Synthetic Supplies Pvt Ltd</LEDGERNAME><ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE><ISPARTYLEDGER>Yes</ISPARTYLEDGER><AMOUNT>1180.00</AMOUNT>" in xml
    assert "<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE><ISPARTYLEDGER>No</ISPARTYLEDGER><AMOUNT>-1000.00</AMOUNT>" in xml
    assert "<REFERENCE>SYN-0001</REFERENCE>" in xml and "<DATE>20260401</DATE>" in xml and "[TA-TEST-INV]" in xml
    # The generated XML is an import: the read-only TallyPrime client refuses to send it.
    with pytest.raises(TallyWriteBlocked):
        assert_read_only(xml)


def test_xml_verification_catches_tampering_and_escapes_names() -> None:
    m = masters()
    plan = plan_invoice(invoice(), [line("1000.00")])
    draft = build_purchase(plan, purchase_ledgers(m), marker="TA-X", document_id=1)
    xml = render_xml(draft, COMPANY)
    assert verify_xml(xml.replace("<AMOUNT>-1000.00</AMOUNT>", "<AMOUNT>-1001.00</AMOUNT>", 1), draft, COMPANY)
    assert verify_xml(xml.replace('ACTION="Create"', 'ACTION="Alter"'), draft, COMPANY)
    assert verify_xml(xml, draft, "OTHER COMPANY")

    bank = build_bank(plan_transaction(1, 1, {"txn_date": f("2026-04-02"), "debit": f("500.00"),
                                              "description": f("NEFT A&B <SYN>\x02 LTD"), "reference_number": f("UTR1")}),
                      BankLedgers(ref(m, "bank"), ref(m, "party"), "Payment", VoucherKind.PAYMENT), marker="TA-T1", document_id=1)
    xml = render_xml(bank, "A & B <TEST>")
    assert "Synthetic Bank &amp; Co" in xml and "<SVCURRENTCOMPANY>A &amp; B &lt;TEST&gt;</SVCURRENTCOMPANY>" in xml
    assert "\x02" not in xml and verify_xml(xml, bank, "A & B <TEST>") == []


def test_bank_transactions_become_payment_receipt_or_contra() -> None:
    m = masters()
    out = plan_transaction(1, 1, {"txn_date": f("2026-04-02"), "debit": f("500.00"), "description": f("NEFT SYNTHETIC")})
    into = plan_transaction(2, 2, {"txn_date": f("2026-04-03"), "credit": f("200.00"), "description": f("CASH DEPOSIT")})
    assert bank_voucher_kind(out, ref(m, "party")) is VoucherKind.PAYMENT
    assert bank_voucher_kind(into, ref(m, "party")) is VoucherKind.RECEIPT
    assert bank_voucher_kind(into, ref(m, "cash")) is VoucherKind.CONTRA

    payment = build_bank(out, BankLedgers(ref(m, "bank"), ref(m, "party"), "Payment", VoucherKind.PAYMENT), marker="M", document_id=1)
    assert [(e.role, e.debit) for e in payment.entries] == [("counter", True), ("bank", False)]
    assert payment.entries[0].bill.bill_type == "On Account"          # bill-wise supplier
    receipt = build_bank(into, BankLedgers(ref(m, "bank"), ref(m, "cash"), "Contra", VoucherKind.CONTRA), marker="M", document_id=1)
    assert [(e.role, e.debit) for e in receipt.entries] == [("bank", True), ("counter", False)]

    both = plan_transaction(3, 3, {"txn_date": f("2026-04-03"), "debit": f("1.00"), "credit": f("1.00")})
    assert not both.buildable and "direction_unclear" in failing(both.checks)


# ---- TallyPrime compatibility ------------------------------------------------------------------------

def test_correct_purchase_passes_every_check() -> None:
    m = masters()
    plan = plan_invoice(invoice(), [line("1000.00")])
    draft = build_purchase(plan, purchase_ledgers(m), marker="M", document_id=1)
    assert failing(purchase_checks(m, draft, plan)) == set()


def test_wrong_ledgers_are_caught() -> None:
    m = masters()
    plan = plan_invoice(invoice(), [line("1000.00")])
    swapped = build_purchase(plan, purchase_ledgers(m, duty={"CGST": ref(m, "igst"), "SGST": ref(m, "sgst")}), marker="M", document_id=1)
    assert "duty_ledger_head" in failing(purchase_checks(m, swapped, plan))
    tax_as_purchase = build_purchase(plan, purchase_ledgers(m, purchase={"18": ref(m, "cgst")}), marker="M", document_id=1)
    assert {"purchase_ledger_kind", "ledger_roles_distinct"} <= failing(purchase_checks(m, tax_as_purchase, plan))
    bank_as_party = build_purchase(plan, purchase_ledgers(m, party=ref(m, "bank")), marker="M", document_id=1)
    assert "party_ledger_kind" in failing(purchase_checks(m, bank_as_party, plan))
    gone = build_purchase(plan, purchase_ledgers(m, party=Ledger("deleted", "Deleted Ledger")), marker="M", document_id=1)
    assert "ledgers_exist" in failing(purchase_checks(m, gone, plan))


def test_gstin_state_type_date_and_voucher_type() -> None:
    m = masters()
    other_gstin = "29BBBBB1111B1Z" + "0"
    plan = plan_invoice(invoice(supplier_gstin=other_gstin), [line("1000.00")])
    draft = build_purchase(plan, purchase_ledgers(m), marker="M", document_id=1)
    problems = failing(purchase_checks(m, draft, plan))
    assert {"party_gstin_matches", "gst_type_for_state"} <= problems     # Karnataka supplier charging CGST/SGST

    early = replace(draft, voucher_date=date(2024, 4, 1))
    assert "date_in_books" in failing(purchase_checks(m, early, plan))
    assert "voucher_type_base" in failing(purchase_checks(m, replace(draft, voucher_type="Payment"), plan))
    assert "voucher_type_active" in failing(purchase_checks(m, replace(draft, voucher_type="Old Purchase"), plan))
    assert "voucher_type_base" not in failing(purchase_checks(m, replace(draft, voucher_type="GST Purchase"), plan))


def test_bank_checks_compare_account_numbers() -> None:
    m = masters()
    plan = plan_transaction(1, 1, {"txn_date": f("2026-04-02"), "debit": f("500.00"), "description": f("X")})
    draft = build_bank(plan, BankLedgers(ref(m, "bank"), ref(m, "party"), "Payment", VoucherKind.PAYMENT), marker="M", document_id=1)
    assert "bank_account_matches" not in failing(bank_checks(m, draft, "1234 5678 9012"))
    assert "bank_account_matches" in failing(bank_checks(m, draft, "999999999999"))
    wrong = build_bank(plan, BankLedgers(ref(m, "party"), ref(m, "cash"), "Contra", VoucherKind.CONTRA), marker="M", document_id=1)
    assert "bank_ledger_kind" in failing(bank_checks(m, wrong, None))


def test_state_names_follow_gst_codes() -> None:
    assert same_state("26AAAAA0000A1Z0", "Dadra & Nagar Haveli and Daman & Diu")
    assert same_state(GSTIN_MH, "maharashtra") and same_state(GSTIN_MH, "Gujarat") is False
    assert same_state(None, "Maharashtra") is None
