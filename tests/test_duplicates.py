"""Level 2/3 duplicate matching rules. SYNTHETIC values only."""

from __future__ import annotations

from extraction_fakes import BUYER_GSTIN, SUPPLIER_GSTIN
from test_validation_accounting import build
from tally_automation.db.models import MatchStrength
from tally_automation.extraction.schemas import INVOICE_FIELDS, TRANSACTION_FIELDS
from tally_automation.validation.duplicates import (
    InvoiceKeys, TransactionKeys, invoice_number_key, lookalike_key, match_invoice, match_transaction,
)


def invoice(entity_id: int, document_id: int, **values: str | None) -> InvoiceKeys:
    base = {"supplier_name": "Synthetic Supplies", "supplier_gstin": SUPPLIER_GSTIN, "invoice_number": "SOS/2026-27/0042",
            "invoice_date": "01/04/2026", "grand_total": "1,180.00"}
    base.update(values)
    return InvoiceKeys(entity_id, document_id, build(INVOICE_FIELDS, base))


def test_number_keys() -> None:
    assert invoice_number_key("sos/2026-27/0042") == "SOS2026270042"
    assert invoice_number_key(" / ") is None
    # The OCR misread seen in Phase 4 ("SOS" read as "s0s") maps to the same look-alike key.
    assert lookalike_key("s0s/2026-27/0042") == lookalike_key("SOS/2026-27/0042")


def test_same_supplier_and_number_is_probable() -> None:
    found = match_invoice(invoice(2, 20, invoice_number="sos-2026-27-0042"), invoice(1, 10))
    assert found is not None
    assert (found.rule, found.strength, found.pair_key) == ("same_invoice_number", MatchStrength.PROBABLE, "invoice:1:2")
    evidence = {e["field"]: e["match"] for e in found.evidence}
    assert evidence == {"supplier_gstin": "same", "invoice_number": "same", "invoice_date": "same",
                        "grand_total": "same", "supplier_name": "same"}


def test_same_number_but_different_amount_is_still_flagged_as_possible_revision() -> None:
    found = match_invoice(invoice(2, 20, grand_total="1,200.00"), invoice(1, 10))
    assert found is not None and found.rule == "same_invoice_number" and "revised" in found.explanation


def test_ocr_lookalike_number() -> None:
    found = match_invoice(invoice(2, 20, invoice_number="s0s/2026-27/0042"), invoice(1, 10))
    assert found is not None and (found.rule, found.strength) == ("invoice_number_variant", MatchStrength.PROBABLE)
    assert next(e for e in found.evidence if e["field"] == "invoice_number")["match"] == "similar"
    weak = match_invoice(invoice(2, 20, invoice_number="s0s/2026-27/0042", invoice_date="02/04/2026",
                                 grand_total="1.00"), invoice(1, 10))
    assert weak is not None and weak.strength is MatchStrength.POSSIBLE


def test_different_suppliers_never_match() -> None:
    assert match_invoice(invoice(2, 20, supplier_gstin=BUYER_GSTIN), invoice(1, 10)) is None


def test_missing_gstin_needs_number_and_date_or_amount() -> None:
    found = match_invoice(invoice(2, 20, supplier_gstin=None), invoice(1, 10))
    assert found is not None and (found.rule, found.strength) == ("number_without_gstin", MatchStrength.PROBABLE)
    assert match_invoice(invoice(2, 20, supplier_gstin=None, invoice_date="09/04/2026", grand_total="9.00"),
                         invoice(1, 10)) is None


def test_same_supplier_date_and_amount_with_new_number_is_possible() -> None:
    found = match_invoice(invoice(2, 20, invoice_number="SYN-0099"), invoice(1, 10))
    assert found is not None and (found.rule, found.strength) == ("same_date_and_amount", MatchStrength.POSSIBLE)
    # Date and amount alone, different supplier number, different date: nothing.
    assert match_invoice(invoice(2, 20, invoice_number="SYN-0099", invoice_date="02/04/2026"), invoice(1, 10)) is None


def txn(entity_id: int, document_id: int, account: str | None = "123456789012", **values: str | None) -> TransactionKeys:
    base = {"txn_date": "05/04/2026", "description": "NEFT SYNTHETIC VENDOR", "reference_number": "UTR123",
            "debit": "5,000.00", "credit": None, "balance": "5,000.00 Cr"}
    base.update(values)
    return TransactionKeys(entity_id, document_id, account, build(TRANSACTION_FIELDS, base))


def test_transaction_fingerprint() -> None:
    a = txn(1, 10)
    assert a.fingerprint() == txn(2, 20, description="neft synthetic-vendor").fingerprint()
    assert a.fingerprint() != txn(3, 30, debit=None, credit="5,000.00").fingerprint()  # direction matters
    assert txn(4, 40, account=None).fingerprint() is None


def test_transaction_rules() -> None:
    other = txn(1, 10)
    assert match_transaction(txn(2, 20), other).rule == "same_fingerprint"
    assert match_transaction(txn(2, 20, description="NEFT SYN VEND"), other).rule == "same_reference"
    assert match_transaction(txn(2, 20, reference_number=None), other).rule == "same_description"
    assert match_transaction(txn(2, 20, reference_number=None, description="X"), other).rule == "same_balance"


def test_same_date_and_amount_alone_is_not_a_duplicate() -> None:
    other = txn(1, 10)
    assert match_transaction(txn(2, 20, reference_number=None, description="OTHER", balance="9.00 Cr"), other) is None
    assert match_transaction(txn(2, 20, account="999999999999"), other) is None
    assert match_transaction(txn(2, 20, debit=None, credit="5,000.00"), other) is None
