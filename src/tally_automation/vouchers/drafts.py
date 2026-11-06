"""Voucher drafts: the accounting entries a voucher will contain.

Pure functions over reviewed extraction fields and reviewer-chosen ledgers.
Nothing is guessed: every amount comes from a value printed on the document
(as extracted or as corrected by a reviewer), and every ledger was chosen by
a person. When the printed amounts do not add up, no voucher is built.

Purchase vouchers use TallyPrime's accounting-invoice form (ledgers only, no
stock items):

    Party (supplier) ledger        Cr  grand total   (bill-wise: New Ref = invoice number)
    Purchase ledger(s)             Dr  taxable value, split by GST rate when the
                                       line items support it exactly
    CGST / SGST / IGST / Cess      Dr  printed tax totals
    Round-off                      Dr or Cr  printed round-off

Bank transactions become Payment (money out), Receipt (money in) or Contra
(the other ledger is a bank or cash ledger) vouchers against the ledger a
reviewer chose. Bill-wise parties are allocated "On Account".

Check category for everything raised here and in ``compat``: ``tally``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from tally_automation.db.models import VoucherKind
from tally_automation.validation.accounting import Check, Severity

CATEGORY = "tally"
ZERO = Decimal("0")
HEADS = ("CGST", "SGST", "IGST", "CESS")
HEAD_FIELDS = {"CGST": "total_cgst", "SGST": "total_sgst", "IGST": "total_igst", "CESS": "total_cess"}
SINGLE_BUCKET = "all"
_USABLE = {"valid", "warning"}

Fields = dict[str, dict[str, Any]]


def check(code: str, severity: Severity, passed: bool, message: str, **kwargs: Any) -> Check:
    return Check(code, CATEGORY, severity, passed, message, **kwargs)


def _value(fields: Fields, name: str) -> tuple[str | None, bool]:
    """(usable value, present-but-unusable)."""
    meta = fields.get(name) or {}
    if meta.get("status") in _USABLE and meta.get("value") is not None:
        return meta["value"], False
    return None, meta.get("raw") not in (None, "")


def _amount(fields: Fields, name: str) -> tuple[Decimal | None, bool]:
    value, unusable = _value(fields, name)
    return (Decimal(value) if value is not None else None), unusable


def money(value: Decimal) -> str:
    return f"{value:.2f}"


def rate_key(rate: Decimal) -> str:
    return format(rate.normalize(), "f")


# ---- Ledger choices --------------------------------------------------------------------------

@dataclass(frozen=True)
class Ledger:
    """A ledger as the reviewer chose it, resolved against the masters snapshot."""

    guid: str
    name: str
    is_bill_wise: bool = False
    is_bank_or_cash: bool = False


@dataclass
class BillAllocation:
    bill_type: str           # "New Ref" | "On Account"
    name: str | None
    amount: Decimal


@dataclass
class Entry:
    ledger: Ledger
    role: str                # party | purchase | duty:CGST | round_off | bank | counter
    debit: bool
    amount: Decimal          # always positive
    bill: BillAllocation | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "ledger": self.ledger.name, "ledger_guid": self.ledger.guid, "role": self.role,
            "debit": money(self.amount) if self.debit else None,
            "credit": None if self.debit else money(self.amount),
            "bill": None if self.bill is None else {
                "type": self.bill.bill_type, "name": self.bill.name, "amount": money(self.bill.amount),
            },
        }


@dataclass
class Draft:
    kind: VoucherKind
    voucher_type: str
    voucher_date: date
    marker: str
    narration: str
    entries: list[Entry]
    reference: str | None = None
    reference_date: date | None = None
    voucher_number: str | None = None
    party: Ledger | None = None
    party_gstin: str | None = None

    @property
    def debit_total(self) -> Decimal:
        return sum((e.amount for e in self.entries if e.debit), ZERO)

    @property
    def credit_total(self) -> Decimal:
        return sum((e.amount for e in self.entries if not e.debit), ZERO)

    @property
    def amount(self) -> Decimal:
        return self.credit_total


def clean_text(text: str | None, limit: int = 1000) -> str:
    """Single-line text without control characters (Tally narration / reference)."""
    if not text:
        return ""
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


# ---- Purchase invoices -------------------------------------------------------------------------

@dataclass
class Bucket:
    key: str                  # GST rate as text ("18", "5", "0") or "all"
    label: str
    taxable: Decimal


@dataclass
class InvoicePlan:
    """What a purchase voucher for this invoice needs, before any ledger is chosen."""

    invoice_number: str | None
    invoice_date: date | None
    supplier_gstin: str | None
    supplier_name: str | None
    grand_total: Decimal | None
    amount_payable: Decimal | None
    taxable: Decimal | None
    taxes: dict[str, Decimal]          # head → printed amount (non-zero only)
    round_off: Decimal
    buckets: list[Bucket]
    line_rates: list[Decimal]          # distinct GST rates on the line items
    checks: list[Check] = field(default_factory=list)

    @property
    def buildable(self) -> bool:
        return not any(c.counts_as_error for c in self.checks)


def _line_rate(line: Fields) -> tuple[Decimal | None, bool]:
    """Effective GST rate of a line (IGST, or CGST + SGST); (None, False) if no rate is printed."""
    rates = {name: _amount(line, name)[0] for name in ("igst_rate", "cgst_rate", "sgst_rate")}
    if rates["igst_rate"]:
        return rates["igst_rate"], True
    if rates["cgst_rate"] is not None and rates["sgst_rate"] is not None:
        return rates["cgst_rate"] + rates["sgst_rate"], True
    if all(r is None for r in rates.values()):
        return None, False
    if rates["igst_rate"] == ZERO and rates["cgst_rate"] is None and rates["sgst_rate"] is None:
        return ZERO, True
    return None, False


def _buckets(taxable: Decimal, lines: list[Fields]) -> tuple[list[Bucket], list[Decimal], Check]:
    by_rate: dict[Decimal, Decimal] = {}
    complete = bool(lines)
    rates: set[Decimal] = set()
    for line in lines:
        amount, _ = _amount(line, "taxable_amount")
        rate, known = _line_rate(line)
        if rate is not None:
            rates.add(rate)
        if amount is None or not known or rate is None:
            complete = False
            continue
        by_rate[rate] = by_rate.get(rate, ZERO) + amount
    single = [Bucket(SINGLE_BUCKET, "All purchases", taxable)]
    if not complete:
        return single, sorted(rates), check(
            "purchase_split", Severity.INFO, False,
            "The purchase amount goes to one purchase ledger: not every line item shows a taxable amount "
            "and a GST rate, so it cannot be split by rate.",
        )
    line_sum = sum(by_rate.values(), ZERO)
    if line_sum != taxable:
        return single, sorted(rates), check(
            "purchase_split", Severity.INFO, False,
            "The purchase amount goes to one purchase ledger: the line items' taxable amounts do not add up "
            "exactly to the printed total taxable value.",
            calculated=money(line_sum), printed=money(taxable), difference=money(line_sum - taxable),
        )
    if len(by_rate) == 1:
        rate = next(iter(by_rate))
        return [Bucket(rate_key(rate), f"Purchases at {rate_key(rate)}% GST", taxable)], sorted(rates), check(
            "purchase_split", Severity.INFO, True, f"All line items are at {rate_key(rate)}% GST.",
        )
    buckets = [Bucket(rate_key(r), f"Purchases at {rate_key(r)}% GST", by_rate[r]) for r in sorted(by_rate)]
    return buckets, sorted(rates), check(
        "purchase_split", Severity.INFO, True,
        f"The purchase amount is split by GST rate ({', '.join(b.key + '%' for b in buckets)}), "
        "from the line items' taxable amounts, which add up exactly to the printed total.",
    )


def plan_invoice(fields: Fields, lines: list[Fields]) -> InvoicePlan:
    checks: list[Check] = []

    def required(name: str, label: str) -> str | None:
        value, unusable = _value(fields, name)
        if value is None:
            checks.append(check(
                f"{name}_required", Severity.ERROR, False,
                f"{label} is {'not readable' if unusable else 'missing'}; a voucher needs it. Correct it on the Review page.",
                fields=[name],
            ))
        return value

    number = required("invoice_number", "The supplier invoice number")
    date_text = required("invoice_date", "The invoice date")
    grand_text = required("grand_total", "The grand total")
    taxable_text = required("total_taxable_value", "The total taxable value")
    grand = Decimal(grand_text) if grand_text else None
    taxable = Decimal(taxable_text) if taxable_text else None

    taxes: dict[str, Decimal] = {}
    not_printed: list[str] = []
    for head, name in HEAD_FIELDS.items():
        amount, unusable = _amount(fields, name)
        if unusable:
            checks.append(check(f"{name}_unreadable", Severity.ERROR, False,
                                f"The printed {head} total is not readable. Correct it on the Review page.", fields=[name]))
        elif amount is None:
            not_printed.append(head)
        elif amount < ZERO:
            checks.append(check(f"{name}_negative", Severity.ERROR, False, f"The {head} total is negative.", fields=[name]))
        elif amount > ZERO:
            taxes[head] = amount
    round_off, unusable = _amount(fields, "round_off")
    if unusable:
        checks.append(check("round_off_unreadable", Severity.ERROR, False,
                            "The printed round-off is not readable. Correct it on the Review page.", fields=["round_off"]))
    round_off = round_off or ZERO
    if not_printed:
        checks.append(check(
            "tax_not_printed", Severity.INFO, True,
            f"No {', '.join(not_printed)} total is printed on the invoice; the voucher has no entry for "
            f"{'it' if len(not_printed) == 1 else 'them'}.",
        ))

    for name, label in (("grand_total", "grand total"), ("total_taxable_value", "taxable value")):
        value = grand if name == "grand_total" else taxable
        if value is not None and value <= ZERO:
            checks.append(check(f"{name}_not_positive", Severity.ERROR, False,
                                f"The {label} must be more than zero for a purchase voucher.", fields=[name]))

    if grand is not None and taxable is not None:
        debit = taxable + sum(taxes.values(), ZERO) + round_off
        checks.append(check(
            "voucher_balances", Severity.ERROR, debit == grand,
            "Taxable value + taxes + round-off equals the grand total, so the voucher balances."
            if debit == grand else
            "Taxable value + taxes + round-off does not equal the grand total, so the voucher would not balance. "
            "Check the printed totals on the Review page.",
            fields=["total_taxable_value", *HEAD_FIELDS.values(), "round_off", "grand_total"],
            calculated=money(debit), printed=money(grand), difference=money(debit - grand),
        ))

    payable, _ = _amount(fields, "amount_payable")
    if payable is not None and grand is not None and payable != grand:
        checks.append(check(
            "amount_payable_differs", Severity.WARNING, False,
            "The amount payable differs from the grand total (for example an advance or TDS). The voucher "
            "credits the supplier with the grand total; record the difference separately in TallyPrime.",
            fields=["amount_payable", "grand_total"], calculated=None, printed=money(payable),
            difference=money(payable - grand),
        ))

    buckets: list[Bucket] = []
    rates: list[Decimal] = []
    if taxable is not None and taxable > ZERO:
        buckets, rates, split = _buckets(taxable, lines)
        checks.append(split)

    gstin, _ = _value(fields, "supplier_gstin")
    name, _ = _value(fields, "supplier_name")
    return InvoicePlan(
        invoice_number=clean_text(number, 255) or None, invoice_date=date.fromisoformat(date_text) if date_text else None,
        supplier_gstin=gstin, supplier_name=name, grand_total=grand, amount_payable=payable, taxable=taxable,
        taxes=taxes, round_off=round_off, buckets=buckets, line_rates=rates, checks=checks,
    )


@dataclass
class PurchaseLedgers:
    voucher_type: str
    party: Ledger
    purchase: dict[str, Ledger]          # bucket key → ledger
    duty: dict[str, Ledger]              # head → ledger (only heads with an amount)
    round_off: Ledger | None


def build_purchase(plan: InvoicePlan, ledgers: PurchaseLedgers, *, marker: str, document_id: int) -> Draft:
    assert plan.buildable and plan.grand_total is not None and plan.invoice_date is not None
    entries: list[Entry] = []
    bill = BillAllocation("New Ref", plan.invoice_number, plan.grand_total) if ledgers.party.is_bill_wise else None
    entries.append(Entry(ledgers.party, "party", False, plan.grand_total, bill))
    merged: dict[str, Entry] = {}
    for bucket in plan.buckets:
        ledger = ledgers.purchase[bucket.key]
        if ledger.guid in merged:
            merged[ledger.guid].amount += bucket.taxable
        else:
            merged[ledger.guid] = Entry(ledger, "purchase", True, bucket.taxable)
    entries.extend(merged.values())
    for head in HEADS:
        if head in plan.taxes:
            entries.append(Entry(ledgers.duty[head], f"duty:{head}", True, plan.taxes[head]))
    if plan.round_off != ZERO:
        assert ledgers.round_off is not None
        entries.append(Entry(ledgers.round_off, "round_off", plan.round_off > ZERO, abs(plan.round_off)))
    narration = clean_text(
        f"Supplier invoice {plan.invoice_number} dated {plan.invoice_date:%d-%m-%Y}. "
        f"Generated by Tally Automation from document #{document_id} [{marker}]"
    )
    return Draft(
        kind=VoucherKind.PURCHASE, voucher_type=ledgers.voucher_type, voucher_date=plan.invoice_date, marker=marker,
        narration=narration, entries=entries, reference=plan.invoice_number, reference_date=plan.invoice_date,
        party=ledgers.party, party_gstin=None,
    )


# ---- Bank transactions -----------------------------------------------------------------------------

@dataclass
class TransactionPlan:
    transaction_id: int
    sequence: int
    txn_date: date | None
    money_out: bool | None             # True: debit (withdrawal); False: credit (deposit)
    amount: Decimal | None
    description: str | None
    reference: str | None
    cheque: str | None
    checks: list[Check] = field(default_factory=list)

    @property
    def buildable(self) -> bool:
        return not any(c.counts_as_error for c in self.checks)


def plan_transaction(transaction_id: int, sequence: int, fields: Fields) -> TransactionPlan:
    checks: list[Check] = []
    date_text, date_bad = _value(fields, "txn_date")
    if date_text is None:
        checks.append(check("txn_date_required", Severity.ERROR, False,
                            f"Transaction {sequence}: the date is {'not readable' if date_bad else 'missing'}.",
                            row=sequence, fields=["txn_date"]))
    debit, debit_bad = _amount(fields, "debit")
    credit, credit_bad = _amount(fields, "credit")
    money_out: bool | None = None
    amount: Decimal | None = None
    if debit_bad or credit_bad:
        checks.append(check("amount_unreadable", Severity.ERROR, False,
                            f"Transaction {sequence}: the debit or credit amount is not readable.",
                            row=sequence, fields=["debit", "credit"]))
    elif bool(debit) == bool(credit):
        checks.append(check(
            "direction_unclear", Severity.ERROR, False,
            f"Transaction {sequence}: exactly one of debit and credit must be more than zero to know whether "
            "money went out or came in.", row=sequence, fields=["debit", "credit"],
        ))
    elif (debit or ZERO) < ZERO or (credit or ZERO) < ZERO:
        checks.append(check("amount_negative", Severity.ERROR, False,
                            f"Transaction {sequence}: amounts must not be negative.", row=sequence, fields=["debit", "credit"]))
    else:
        money_out = bool(debit)
        amount = debit if money_out else credit
    description, _ = _value(fields, "description")
    if not description:
        checks.append(check("description_missing", Severity.WARNING, False,
                            f"Transaction {sequence}: there is no description for the narration.",
                            row=sequence, fields=["description"]))
    reference, _ = _value(fields, "reference_number")
    cheque, _ = _value(fields, "cheque_number")
    return TransactionPlan(
        transaction_id=transaction_id, sequence=sequence,
        txn_date=date.fromisoformat(date_text) if date_text else None, money_out=money_out, amount=amount,
        description=description, reference=reference, cheque=cheque, checks=checks,
    )


@dataclass
class BankLedgers:
    bank: Ledger
    counter: Ledger
    voucher_type: str
    kind: VoucherKind


def bank_voucher_kind(plan: TransactionPlan, counter: Ledger) -> VoucherKind:
    if counter.is_bank_or_cash:
        return VoucherKind.CONTRA
    return VoucherKind.PAYMENT if plan.money_out else VoucherKind.RECEIPT


def build_bank(plan: TransactionPlan, ledgers: BankLedgers, *, marker: str, document_id: int) -> Draft:
    assert plan.buildable and plan.amount is not None and plan.txn_date is not None and plan.money_out is not None
    bill = BillAllocation("On Account", None, plan.amount) if ledgers.counter.is_bill_wise else None
    counter = Entry(ledgers.counter, "counter", plan.money_out, plan.amount, bill)
    bank = Entry(ledgers.bank, "bank", not plan.money_out, plan.amount)
    entries = [counter, bank] if plan.money_out else [bank, counter]
    details = [clean_text(plan.description, 500)]
    if plan.reference:
        details.append(f"Ref {clean_text(plan.reference, 64)}")
    if plan.cheque:
        details.append(f"Chq {clean_text(plan.cheque, 32)}")
    narration = clean_text(
        " | ".join(d for d in details if d) + f". Generated by Tally Automation from document #{document_id} [{marker}]"
    )
    return Draft(
        kind=ledgers.kind, voucher_type=ledgers.voucher_type, voucher_date=plan.txn_date, marker=marker,
        narration=narration, entries=entries, reference=clean_text(plan.reference or plan.cheque, 255) or None,
        party=ledgers.counter,
    )
