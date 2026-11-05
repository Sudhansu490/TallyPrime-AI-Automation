"""Business-level duplicate detection (Levels 2 and 3).

Level 1 (byte-identical files, SHA-256) happens at upload. The levels here
compare *extracted, reviewed* values across documents, so they also catch the
same invoice scanned differently or appearing in a different PDF, and the same
bank transaction in overlapping statements.

Rules never reject anything. Each match is stored with its evidence (field by
field: same / similar / different / unknown) for a person to decide. Two
records that merely share a date and an amount are *not* flagged: that is
common for genuine transactions.

Level 2 – invoices (same supplier = same GSTIN when both are known):

* ``same_invoice_number``      same supplier, same invoice number → probable
* ``invoice_number_variant``   same supplier, number equal after mapping
  OCR look-alikes (O/0, I/L/1, S/5, B/8, Z/2) → probable if date or amount
  also match, else possible
* ``number_without_gstin``     a GSTIN is missing, number (or variant) equal and
  date or amount equal → probable if both, else possible
* ``same_date_and_amount``     same supplier, same date and grand total,
  different number → possible

Level 3 – bank transactions of the same account in different statements:

* ``same_fingerprint``         same date, direction, amount, reference and
  description → probable
* ``same_reference``           same date, direction, amount and reference → probable
* ``same_description``         same date, direction, amount and description → probable
* ``same_balance``             same date, direction, amount and resulting balance → probable
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from tally_automation.db.models import DuplicateKind, MatchStrength
from tally_automation.validation.accounting import amount, day, money, normalized_description, text

_LOOKALIKE = str.maketrans({"O": "0", "D": "0", "Q": "0", "I": "1", "L": "1", "S": "5", "B": "8", "Z": "2"})


def invoice_number_key(value: str | None) -> str | None:
    """Upper-case letters and digits only: 'inv/2026-27/0042' → 'INV2026270042'."""
    key = re.sub(r"[^A-Z0-9]", "", (value or "").upper())
    return key or None


def lookalike_key(value: str | None) -> str | None:
    key = invoice_number_key(value)
    return key.translate(_LOOKALIKE) if key else None


def _name_key(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


@dataclass
class Found:
    """One detected match from the point of view of a new record."""

    kind: DuplicateKind
    entity_id: int
    other_entity_id: int
    other_document_id: int
    strength: MatchStrength
    rule: str
    explanation: str
    evidence: list[dict[str, Any]] = field(default_factory=list)

    @property
    def pair_key(self) -> str:
        low, high = sorted((self.entity_id, self.other_entity_id))
        return f"{self.kind.value}:{low}:{high}"


def _evidence(field_name: str, label: str, this: Any, other: Any, match: str | None = None) -> dict[str, Any]:
    if match is None:
        match = "unknown" if this is None or other is None else ("same" if this == other else "different")
    shown = lambda v: v.isoformat() if isinstance(v, date) else (money(v) if isinstance(v, Decimal) else v)  # noqa: E731
    return {"field": field_name, "label": label, "this": shown(this), "other": shown(other), "match": match}


# ---- Level 2: invoices -------------------------------------------------------------------

@dataclass(frozen=True)
class InvoiceKeys:
    invoice_id: int
    document_id: int
    fields: dict[str, dict[str, Any]]

    @property
    def gstin(self) -> str | None:
        return text(self.fields, "supplier_gstin")

    @property
    def number(self) -> str | None:
        return text(self.fields, "invoice_number")

    @property
    def date(self) -> date | None:
        return day(self.fields, "invoice_date")

    @property
    def total(self) -> Decimal | None:
        return amount(self.fields, "grand_total")

    @property
    def supplier_name(self) -> str | None:
        return text(self.fields, "supplier_name")


def match_invoice(this: InvoiceKeys, other: InvoiceKeys) -> Found | None:
    number_same = invoice_number_key(this.number) is not None and invoice_number_key(this.number) == invoice_number_key(other.number)
    number_variant = (not number_same and lookalike_key(this.number) is not None
                      and lookalike_key(this.number) == lookalike_key(other.number))
    gstin_known = this.gstin is not None and other.gstin is not None
    same_gstin = gstin_known and this.gstin == other.gstin
    date_same = this.date is not None and this.date == other.date
    total_same = this.total is not None and other.total is not None and this.total == other.total

    if gstin_known and not same_gstin:
        return None      # different suppliers (both GSTINs passed the checksum)
    if same_gstin and number_same:
        rule, strength = "same_invoice_number", MatchStrength.PROBABLE
        explanation = "Same supplier GSTIN and the same invoice number."
    elif same_gstin and number_variant:
        rule = "invoice_number_variant"
        strength = MatchStrength.PROBABLE if (date_same or total_same) else MatchStrength.POSSIBLE
        explanation = ("Same supplier GSTIN; the invoice numbers differ only in characters OCR often confuses "
                       "(e.g. O/0, S/5, I/1).")
    elif not gstin_known and (number_same or number_variant) and (date_same or total_same):
        rule = "number_without_gstin"
        strength = MatchStrength.PROBABLE if (date_same and total_same and number_same) else MatchStrength.POSSIBLE
        matched = " and ".join(n for n, ok in (("date", date_same), ("amount", total_same)) if ok)
        explanation = ("Supplier GSTIN is missing on at least one invoice; the invoice number "
                       f"{'matches' if number_same else 'nearly matches (OCR look-alikes)'}, and so does the {matched}.")
    elif same_gstin and date_same and total_same:
        rule, strength = "same_date_and_amount", MatchStrength.POSSIBLE
        explanation = ("Same supplier, invoice date and grand total, but a different invoice number. "
                       "Could be a re-numbered copy, or a genuine second invoice.")
    else:
        return None

    number_match = "same" if number_same else "similar" if number_variant else None
    evidence = [
        _evidence("supplier_gstin", "Supplier GSTIN", this.gstin, other.gstin),
        _evidence("invoice_number", "Invoice number", this.number, other.number, number_match),
        _evidence("invoice_date", "Invoice date", this.date, other.date),
        _evidence("grand_total", "Grand total", this.total, other.total),
        _evidence("supplier_name", "Supplier name", this.supplier_name, other.supplier_name,
                  None if this.supplier_name is None or other.supplier_name is None else
                  "same" if _name_key(this.supplier_name) == _name_key(other.supplier_name) else "different"),
    ]
    if same_gstin and number_same and not (date_same and total_same):
        explanation += " The date or amount differs: possibly a revised invoice; check both."
    return Found(
        DuplicateKind.INVOICE, this.invoice_id, other.invoice_id, other.document_id, strength, rule,
        explanation, evidence,
    )


# ---- Level 3: bank transactions ------------------------------------------------------------

@dataclass(frozen=True)
class TransactionKeys:
    transaction_id: int
    document_id: int
    account: str | None
    fields: dict[str, dict[str, Any]]

    @property
    def date(self) -> date | None:
        return day(self.fields, "txn_date")

    @property
    def direction_amount(self) -> tuple[str, Decimal] | None:
        debit, credit = amount(self.fields, "debit"), amount(self.fields, "credit")
        if debit and not credit:
            return ("debit", debit)
        if credit and not debit:
            return ("credit", credit)
        return None

    @property
    def reference(self) -> str | None:
        return invoice_number_key(text(self.fields, "reference_number"))

    @property
    def description(self) -> str:
        return normalized_description(text(self.fields, "description"))

    @property
    def balance(self) -> Decimal | None:
        return amount(self.fields, "balance")

    def fingerprint(self) -> str | None:
        """SHA-256 over account, date, direction, amount, reference and description."""
        movement = self.direction_amount
        if self.account is None or self.date is None or movement is None:
            return None
        parts = [self.account, self.date.isoformat(), movement[0], money(movement[1]), self.reference or "",
                 self.description]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()


def match_transaction(this: TransactionKeys, other: TransactionKeys) -> Found | None:
    movement = this.direction_amount
    if (this.account is None or this.account != other.account or this.date is None or this.date != other.date
            or movement is None or movement != other.direction_amount):
        return None
    same_reference = this.reference is not None and this.reference == other.reference
    same_description = bool(this.description) and this.description == other.description
    same_balance = this.balance is not None and this.balance == other.balance
    if this.fingerprint() == other.fingerprint():
        rule, explanation = "same_fingerprint", "Same account, date, amount, direction, reference and description."
    elif same_reference:
        rule, explanation = "same_reference", "Same account, date, amount, direction and reference number."
    elif same_description:
        rule, explanation = "same_description", "Same account, date, amount, direction and description."
    elif same_balance:
        rule, explanation = "same_balance", ("Same account, date, amount and direction, and the same balance after "
                                             "the transaction; the description was read differently.")
    else:
        return None      # date and amount alone are not evidence of a duplicate
    explanation += " Typical of overlapping statement periods."
    direction, value = movement
    other_movement = other.direction_amount
    evidence = [
        _evidence("account_number", "Account", "same account", "same account", "same"),
        _evidence("txn_date", "Date", this.date, other.date),
        _evidence(direction, direction.capitalize(), value, other_movement[1] if other_movement else None),
        _evidence("reference_number", "Reference", text(this.fields, "reference_number"),
                  text(other.fields, "reference_number"),
                  None if this.reference is None or other.reference is None else
                  "same" if same_reference else "different"),
        _evidence("description", "Description", text(this.fields, "description"), text(other.fields, "description"),
                  "same" if same_description else "different"),
        _evidence("balance", "Balance after", this.balance, other.balance),
    ]
    return Found(
        DuplicateKind.BANK_TRANSACTION, this.transaction_id, other.transaction_id, other.document_id,
        MatchStrength.PROBABLE, rule, explanation, evidence,
    )
