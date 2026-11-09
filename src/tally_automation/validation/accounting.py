"""Extraction and accounting validation of one extracted document.

Pure functions over the stored field metadata (see ``db.models.extraction``).
Nothing here changes a value: every rule reports what it calculated, what
was printed and the difference, and a person decides what to do.

Two categories (the spec's third, TallyPrime compatibility, arrives with
voucher generation in Phase 7):

* ``extraction`` – the transcription itself: invalid or missing required
  values, unresolved conflicts, low confidence.
* ``accounting`` – arithmetic and consistency: line and invoice totals, GST
  components, CGST/SGST versus IGST, debit/credit, running balances.

Severities: ``error`` blocks voucher generation until fixed (by correcting a
mis-transcribed value on the Review page); ``warning`` must be read but does
not block; ``info`` explains a check that could not run or a presentation
choice. Calculated values appear only in check results, marked as such; they
are never written into the extracted fields.

Amounts are exact ``Decimal`` values. Tolerances are explicit: ₹0.01 for a
single calculated value (tax rounding), ₹0.01 per line for sums of lines, and
none at all for bank running balances.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any

from tally_automation.extraction.schemas import (
    INVOICE_FIELDS, LINE_ITEM_FIELDS, STATEMENT_FIELDS, TRANSACTION_FIELDS, FieldSpec,
)

RULES_VERSION = "2026-11-09.2"

PAISA = Decimal("0.01")
ZERO = Decimal("0")
ROUND_OFF_LIMIT = Decimal("1.00")
_USABLE = {"valid", "warning"}

Fields = dict[str, dict[str, Any]]


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass
class Check:
    code: str
    category: str              # "extraction" | "accounting"
    severity: Severity         # how serious it is when it does not pass
    passed: bool
    message: str
    row: int | None = None     # line item / transaction number, None for the header
    fields: list[str] = field(default_factory=list)
    calculated: str | None = None   # value derived by this check (never stored as a field)
    printed: str | None = None      # value as extracted / edited
    difference: str | None = None

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["severity"] = self.severity.value
        return data

    @property
    def counts_as_error(self) -> bool:
        return not self.passed and self.severity is Severity.ERROR

    @property
    def counts_as_warning(self) -> bool:
        return not self.passed and self.severity is Severity.WARNING


# ---- Value access --------------------------------------------------------------------

def _meta(fields: Fields, name: str) -> dict[str, Any]:
    return fields.get(name) or {}


def amount(fields: Fields, name: str) -> Decimal | None:
    """Parsed value of a usable field, or None (missing or not interpretable)."""
    meta = _meta(fields, name)
    value = meta.get("value")
    if value is None or meta.get("status") not in _USABLE:
        return None
    return Decimal(value)


def day(fields: Fields, name: str) -> date | None:
    meta = _meta(fields, name)
    value = meta.get("value")
    if value is None or meta.get("status") not in _USABLE:
        return None
    return date.fromisoformat(value)


def text(fields: Fields, name: str) -> str | None:
    meta = _meta(fields, name)
    return meta.get("value") if meta.get("status") in _USABLE else None


def money(value: Decimal | None) -> str | None:
    return None if value is None else f"{value.quantize(PAISA, ROUND_HALF_UP)}"


def round2(value: Decimal) -> Decimal:
    return value.quantize(PAISA, ROUND_HALF_UP)


def _compare(
    checks: list[Check], code: str, *, calculated: Decimal, printed: Decimal, tolerance: Decimal,
    severity: Severity, ok: str, bad: str, row: int | None = None, fields: list[str] | None = None,
) -> bool:
    difference = printed - calculated
    passed = abs(difference) <= tolerance
    checks.append(Check(
        code=code, category="accounting", severity=severity, passed=passed,
        message=ok if passed else bad, row=row, fields=fields or [],
        calculated=money(calculated), printed=money(printed), difference=money(difference),
    ))
    return passed


# ---- Extraction checks -----------------------------------------------------------------

_LABELS = {spec.name: spec.label for spec in (
    *INVOICE_FIELDS, *LINE_ITEM_FIELDS, *STATEMENT_FIELDS, *TRANSACTION_FIELDS,
)}


def extraction_checks(
    specs: tuple[FieldSpec, ...], fields: Fields, *, row: int | None = None,
) -> list[Check]:
    """Status of the transcription of each field (set by Phase 4 parsing and edits)."""
    checks: list[Check] = []
    where = f"Row {row}: " if row is not None else ""
    for spec in specs:
        meta = _meta(fields, spec.name)
        flags = meta.get("flags") or []
        detail = " ".join(meta.get("messages") or [])
        label = f"{where}{spec.label}"

        def add(code: str, severity: Severity, message: str) -> None:
            checks.append(Check(
                code=code, category="extraction", severity=severity, passed=False,
                message=message, row=row, fields=[spec.name], printed=meta.get("raw"),
            ))

        if "required_missing" in flags:
            add("required_missing", Severity.ERROR, f"{label} is required but was not found.")
        if meta.get("status") == "invalid":
            add("value_invalid", Severity.ERROR, f"{label} cannot be used as read: {detail}".strip())
        if "conflict" in flags:
            add("value_conflict", Severity.ERROR,
                f"{label}: different values were read on different pages; confirm the right one by editing it.")
        if meta.get("status") == "warning":
            add("value_warning", Severity.WARNING, f"{label}: {detail}".strip())
        if "low_confidence" in flags or "low_ocr_confidence" in flags:
            add("low_confidence", Severity.WARNING, f"{label} was read with low confidence; compare it with the document.")
    return checks


# ---- Purchase invoices -------------------------------------------------------------------

TAXES = (("cgst", "CGST"), ("sgst", "SGST"), ("igst", "IGST"))


@dataclass
class _LineTax:
    amounts: dict[str, Decimal | None]   # per tax kind: printed amount, else calculated, else None
    calculated: set[str]                 # tax kinds whose amount was calculated from the rate


def _line_checks(number: int, f: Fields, checks: list[Check]) -> _LineTax:
    taxable = amount(f, "taxable_amount")
    quantity, rate, discount = amount(f, "quantity"), amount(f, "rate"), amount(f, "discount")

    # Taxable value = quantity × rate − discount. A warning only: some invoices
    # print rates inclusive of tax or per a different unit; the printed taxable
    # value is what the accounts use.
    if taxable is not None and quantity is not None and rate is not None:
        _compare(
            checks, "line_taxable_value", calculated=round2(quantity * rate - (discount or ZERO)),
            printed=taxable, tolerance=PAISA, severity=Severity.WARNING, row=number,
            fields=["quantity", "rate", "discount", "taxable_amount"],
            ok="Taxable value = quantity × rate − discount.",
            bad="Taxable value differs from quantity × rate − discount.",
        )

    tax = _LineTax({}, set())
    for kind, label in TAXES:
        rate_pct, printed = amount(f, f"{kind}_rate"), amount(f, f"{kind}_amount")
        tax.amounts[kind] = printed
        if taxable is None or rate_pct is None:
            continue
        calculated = round2(taxable * rate_pct / 100)
        if printed is None:
            tax.amounts[kind] = calculated
            tax.calculated.add(kind)
            continue
        _compare(
            checks, f"line_{kind}_amount", calculated=calculated, printed=printed, tolerance=PAISA,
            severity=Severity.ERROR, row=number, fields=["taxable_amount", f"{kind}_rate", f"{kind}_amount"],
            ok=f"{label} = taxable value × {rate_pct}%.",
            bad=f"{label} amount does not equal taxable value × {rate_pct}%.",
        )

    cgst_rate, sgst_rate = amount(f, "cgst_rate"), amount(f, "sgst_rate")
    has = {kind: (tax.amounts[kind] or ZERO) != 0 for kind, _ in TAXES}
    if has["igst"] and (has["cgst"] or has["sgst"]):
        checks.append(Check(
            "line_igst_with_cgst", "accounting", Severity.ERROR, False,
            "A line cannot carry both IGST and CGST/SGST.", row=number,
            fields=["cgst_amount", "sgst_amount", "igst_amount"],
        ))
    if cgst_rate is not None and sgst_rate is not None and cgst_rate != sgst_rate:
        checks.append(Check(
            "line_cgst_sgst_rate", "accounting", Severity.ERROR, False,
            f"CGST rate ({cgst_rate}%) and SGST rate ({sgst_rate}%) must be equal.", row=number,
            fields=["cgst_rate", "sgst_rate"],
        ))
    elif has["cgst"] != has["sgst"]:
        checks.append(Check(
            "line_cgst_sgst_pair", "accounting", Severity.ERROR, False,
            "CGST and SGST must be charged together.", row=number, fields=["cgst_amount", "sgst_amount"],
        ))

    line_total = amount(f, "line_total")
    if line_total is not None and taxable is not None:
        if line_total == taxable:
            checks.append(Check(
                "line_total", "accounting", Severity.INFO, True,
                "Line total is printed before tax (equals the taxable value).", row=number,
                fields=["line_total"], printed=money(line_total),
            ))
        else:
            calculated = taxable + sum((tax.amounts[k] or ZERO) for k, _ in TAXES) + (amount(f, "cess_amount") or ZERO)
            _compare(
                checks, "line_total", calculated=calculated, printed=line_total, tolerance=PAISA,
                severity=Severity.WARNING, row=number, fields=["line_total"],
                ok="Line total = taxable value + tax.", bad="Line total differs from taxable value + tax.",
            )
    return tax


def _sum_check(
    checks: list[Check], code: str, label: str, printed: Decimal | None, parts: list[Decimal | None],
    *, field_name: str, calculated_note: str = "",
) -> Decimal | None:
    """Compare a header total with the sum of the lines. Returns the best total to use."""
    if not parts:
        return printed
    if any(p is None for p in parts):
        if printed is not None:
            checks.append(Check(
                code, "accounting", Severity.INFO, True,
                f"{label}: not every line shows this amount, so the printed total could not be checked against the lines.",
                fields=[field_name], printed=money(printed),
            ))
        return printed
    total = sum(parts, ZERO)
    if printed is None:
        if total != 0:
            checks.append(Check(
                code, "accounting", Severity.INFO, True,
                f"{label} is not printed; the lines add up to {money(total)} (calculated).{calculated_note}",
                fields=[field_name], calculated=money(total),
            ))
        return total
    _compare(
        checks, code, calculated=total, printed=printed, tolerance=PAISA * len(parts),
        severity=Severity.ERROR, fields=[field_name],
        ok=f"{label} equals the sum of the line items.{calculated_note}",
        bad=f"{label} does not equal the sum of the line items.{calculated_note}",
    )
    return printed


def _gst_state(fields: Fields, name: str) -> str | None:
    gstin = text(fields, name)
    return gstin[:2] if gstin else None


def invoice_checks(header: Fields, lines: list[Fields], *, today: date) -> list[Check]:
    checks: list[Check] = []
    checks += extraction_checks(INVOICE_FIELDS, header)
    for number, line in enumerate(lines, start=1):
        checks += extraction_checks(LINE_ITEM_FIELDS, line, row=number)

    if not lines:
        checks.append(Check(
            "no_line_items", "accounting", Severity.WARNING, False,
            "No line items were extracted, so totals cannot be checked against lines.",
        ))
    line_taxes = [_line_checks(n, line, checks) for n, line in enumerate(lines, start=1)]

    # ---- Header totals against the lines
    taxable = _sum_check(
        checks, "total_taxable_value", "Total taxable value", amount(header, "total_taxable_value"),
        [amount(line, "taxable_amount") for line in lines], field_name="total_taxable_value",
    )
    tax_totals: dict[str, Decimal | None] = {}
    for kind, label in TAXES:
        parts = [t.amounts[kind] for t in line_taxes]
        printed = amount(header, f"total_{kind}")
        if all(p is None for p in parts):
            tax_totals[kind] = printed
            continue
        parts = [p if p is not None else ZERO for p in parts]  # a line without this tax pays none
        used_rates = any(kind in t.calculated for t in line_taxes)
        tax_totals[kind] = _sum_check(
            checks, f"total_{kind}", f"Total {label}", printed, parts, field_name=f"total_{kind}",
            calculated_note=" Some line amounts were calculated from their rates." if used_rates else "",
        )
    cess_parts = [amount(line, "cess_amount") for line in lines]
    cess = amount(header, "total_cess")
    if any(p is not None for p in cess_parts):
        cess = _sum_check(
            checks, "total_cess", "Total cess", cess, [p or ZERO for p in cess_parts], field_name="total_cess",
        )

    # ---- Subtotal: printed either before or after discount; accept both.
    subtotal, discount = amount(header, "subtotal"), amount(header, "total_discount")
    if subtotal is not None and taxable is not None:
        passed = subtotal == taxable or (discount is not None and subtotal - discount == taxable)
        checks.append(Check(
            "subtotal", "accounting", Severity.WARNING, passed,
            "Subtotal is consistent with the taxable value." if passed
            else "Subtotal matches neither the taxable value nor taxable value + discount.",
            fields=["subtotal", "total_discount", "total_taxable_value"],
            calculated=money(taxable), printed=money(subtotal),
        ))

    # ---- Grand total
    grand_total = amount(header, "grand_total")
    round_off = amount(header, "round_off")
    if grand_total is not None and grand_total <= 0:
        checks.append(Check(
            "grand_total_positive", "accounting", Severity.ERROR, False,
            "Grand total must be greater than zero for a purchase invoice.", fields=["grand_total"],
            printed=money(grand_total),
        ))
    if grand_total is not None and taxable is not None:
        before_round = taxable + sum((tax_totals[k] or ZERO) for k, _ in TAXES) + (cess or ZERO)
        if round_off is not None:
            _compare(
                checks, "grand_total", calculated=before_round + round_off, printed=grand_total,
                tolerance=PAISA, severity=Severity.ERROR,
                fields=["grand_total", "total_taxable_value", "total_cgst", "total_sgst", "total_igst", "round_off"],
                ok="Grand total = taxable value + GST + cess + round-off.",
                bad="Grand total does not equal taxable value + GST + cess + round-off.",
            )
        else:
            difference = grand_total - before_round
            if abs(difference) <= PAISA:
                passed, severity, message = True, Severity.ERROR, "Grand total = taxable value + GST + cess."
            elif abs(difference) < ROUND_OFF_LIMIT and grand_total == grand_total.to_integral_value():
                passed, severity, message = False, Severity.WARNING, (
                    "Grand total differs by less than ₹1 and is a whole-rupee amount, consistent with rounding, "
                    "but no round-off is printed."
                )
            else:
                passed, severity, message = False, Severity.ERROR, "Grand total does not equal taxable value + GST + cess."
            checks.append(Check(
                "grand_total", "accounting", severity, passed, message,
                fields=["grand_total", "total_taxable_value", "total_cgst", "total_sgst", "total_igst"],
                calculated=money(before_round), printed=money(grand_total), difference=money(difference),
            ))
    elif grand_total is not None:
        checks.append(Check(
            "grand_total", "accounting", Severity.WARNING, False,
            "Grand total could not be checked: the taxable value is neither printed nor available on every line.",
            fields=["grand_total"], printed=money(grand_total),
        ))
    if round_off is not None and abs(round_off) >= ROUND_OFF_LIMIT:
        checks.append(Check(
            "round_off_size", "accounting", Severity.WARNING, False,
            "Round-off is ₹1 or more; rounding to whole rupees never needs that much.",
            fields=["round_off"], printed=money(round_off),
        ))
    payable = amount(header, "amount_payable")
    if payable is not None and grand_total is not None and payable != grand_total:
        checks.append(Check(
            "amount_payable", "accounting", Severity.WARNING, False,
            "Amount payable differs from the grand total (e.g. advance or deduction); confirm which amount to record.",
            fields=["amount_payable", "grand_total"], calculated=money(grand_total), printed=money(payable),
            difference=money(payable - grand_total),
        ))

    # ---- CGST/SGST (same state) versus IGST (different states)
    has_igst = (tax_totals["igst"] or ZERO) != 0
    has_cgst = (tax_totals["cgst"] or ZERO) != 0 or (tax_totals["sgst"] or ZERO) != 0
    supplier_state = _gst_state(header, "supplier_gstin")
    supply_state = text(header, "state_code") or _gst_state(header, "buyer_gstin")
    if has_igst and has_cgst:
        checks.append(Check(
            "supply_type", "accounting", Severity.ERROR, False,
            "The invoice charges both IGST and CGST/SGST.", fields=["total_cgst", "total_sgst", "total_igst"],
        ))
    elif supplier_state and supply_state and (has_igst or has_cgst):
        intra = supplier_state == supply_state
        passed = (intra and has_cgst) or (not intra and has_igst)
        where = f"supplier state {supplier_state}, place of supply {supply_state}"
        checks.append(Check(
            "supply_type", "accounting", Severity.WARNING, passed,
            (f"Tax type matches the supply ({where})." if passed else
             f"{'Same' if intra else 'Different'} states ({where}) normally mean "
             f"{'CGST + SGST' if intra else 'IGST'}, but the invoice charges "
             f"{'IGST' if has_igst else 'CGST + SGST'}. Exceptions exist (e.g. SEZ supplies); confirm."),
            fields=["supplier_gstin", "state_code", "buyer_gstin"],
        ))
    elif has_igst or has_cgst:
        checks.append(Check(
            "supply_type", "accounting", Severity.INFO, True,
            "CGST/SGST versus IGST could not be checked: the supplier state or place of supply is unknown.",
            fields=["supplier_gstin", "state_code", "buyer_gstin"],
        ))

    supplier, buyer = text(header, "supplier_gstin"), text(header, "buyer_gstin")
    if supplier and buyer and supplier == buyer:
        checks.append(Check(
            "same_gstin", "accounting", Severity.ERROR, False,
            "Supplier and buyer GSTIN are the same.", fields=["supplier_gstin", "buyer_gstin"],
        ))

    # ---- Dates
    invoice_date, due_date = day(header, "invoice_date"), day(header, "due_date")
    if invoice_date and invoice_date > today:
        checks.append(Check(
            "invoice_date_future", "accounting", Severity.ERROR, False,
            "Invoice date is in the future.", fields=["invoice_date"], printed=invoice_date.isoformat(),
        ))
    if invoice_date and due_date and due_date < invoice_date:
        checks.append(Check(
            "due_date_order", "accounting", Severity.WARNING, False,
            "Due date is before the invoice date.", fields=["invoice_date", "due_date"],
        ))
    return checks


# ---- Bank statements ----------------------------------------------------------------------

def normalized_description(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def _order(dates: list[date | None]) -> str:
    known = [d for d in dates if d is not None]
    if all(a <= b for a, b in zip(known, known[1:])):
        return "ascending"
    if all(a >= b for a, b in zip(known, known[1:])):
        return "descending"
    return "mixed"


def _running_balance(
    numbered: list[tuple[int, Fields]], opening: Decimal | None,
) -> tuple[list[Check], int, int]:
    """Balance checks over rows in chronological order: (failed checks, checked, mismatched)."""
    checks: list[Check] = []
    previous = opening
    checked = mismatched = 0
    for number, row in numbered:
        debit, credit, balance = amount(row, "debit") or ZERO, amount(row, "credit") or ZERO, amount(row, "balance")
        if previous is None:
            previous = balance          # no opening balance: start from the first printed balance
            continue
        expected = previous - debit + credit
        if balance is None:
            previous = expected         # carry the calculated balance forward
            continue
        checked += 1
        if balance != expected:
            mismatched += 1
            swapped = previous + debit - credit == balance
            if not debit and not credit:
                # Only a hint for the reviewer: the value is never filled in automatically.
                gap = balance - previous
                message = (
                    "No debit or credit was read for this row. The balances fit a "
                    f"{'credit' if gap > 0 else 'debit'} of {money(abs(gap))}: compare with the statement "
                    "and enter the amount."
                )
            elif swapped:
                message = "Balance matches only if debit and credit are swapped: the amount is probably in the wrong column."
            else:
                message = "Balance does not equal the previous balance − debit + credit."
            checks.append(Check(
                "running_balance", "accounting", Severity.ERROR, False,
                message,
                row=number, fields=["debit", "credit", "balance"],
                calculated=money(expected), printed=money(balance), difference=money(balance - expected),
            ))
        previous = balance              # re-synchronise so one error does not cascade
    return checks, checked, mismatched


def statement_checks(header: Fields, rows: list[Fields], *, today: date) -> list[Check]:
    checks: list[Check] = []
    checks += extraction_checks(STATEMENT_FIELDS, header)
    for number, row in enumerate(rows, start=1):
        checks += extraction_checks(TRANSACTION_FIELDS, row, row=number)

    period_from, period_to = day(header, "period_from"), day(header, "period_to")
    if period_from and period_to and period_from > period_to:
        checks.append(Check(
            "period_order", "accounting", Severity.ERROR, False,
            "Statement period starts after it ends.", fields=["period_from", "period_to"],
        ))
    if not rows:
        checks.append(Check(
            "no_transactions", "accounting", Severity.ERROR, False,
            "No transactions were extracted, so there is nothing to post. If the statement lists "
            "transactions, reprocess it; a statement with no activity needs no vouchers.",
        ))

    # ---- Each transaction: exactly one of debit/credit, date present and in the period
    for number, row in enumerate(rows, start=1):
        debit, credit = amount(row, "debit"), amount(row, "credit")
        if (debit or ZERO) > 0 and (credit or ZERO) > 0:
            checks.append(Check(
                "debit_and_credit", "accounting", Severity.ERROR, False,
                "Both debit and credit are filled in.", row=number, fields=["debit", "credit"],
            ))
        elif not (debit or ZERO) and not (credit or ZERO):
            checks.append(Check(
                "no_amount", "accounting", Severity.ERROR, False,
                "Neither a debit nor a credit amount.", row=number, fields=["debit", "credit"],
            ))
        if (debit is not None and debit < 0) or (credit is not None and credit < 0):
            checks.append(Check(
                "negative_amount", "accounting", Severity.WARNING, False,
                "Negative debit or credit amount; confirm the direction.", row=number, fields=["debit", "credit"],
            ))
        txn_date = day(row, "txn_date")
        if txn_date is None:
            if amount(row, "debit") is not None or amount(row, "credit") is not None:
                checks.append(Check(
                    "txn_date_missing", "accounting", Severity.ERROR, False,
                    "Transaction date is missing or unreadable.", row=number, fields=["txn_date"],
                ))
        else:
            if txn_date > today:
                checks.append(Check(
                    "txn_date_future", "accounting", Severity.ERROR, False,
                    "Transaction date is in the future.", row=number, fields=["txn_date"],
                ))
            if (period_from and txn_date < period_from) or (period_to and txn_date > period_to):
                checks.append(Check(
                    "txn_outside_period", "accounting", Severity.WARNING, False,
                    "Transaction date is outside the statement period.", row=number, fields=["txn_date"],
                ))

    # ---- Running balance, in chronological order
    dates = [day(r, "txn_date") for r in rows]
    order = _order(dates)
    opening, closing = amount(header, "opening_balance"), amount(header, "closing_balance")
    numbered = list(enumerate(rows, start=1))
    if order == "descending":
        numbered.reverse()
        checks.append(Check(
            "row_order", "accounting", Severity.INFO, True,
            "Transactions are listed newest first; balances were checked from the bottom up.",
        ))
    elif order == "mixed":
        checks.append(Check(
            "row_order", "accounting", Severity.WARNING, False,
            "Transaction dates are not in order; balances were checked in the printed order.",
        ))
    elif len({d for d in dates if d is not None}) <= 1 and len(rows) > 1:
        # The dates cannot tell the order (all on one day): the printed balances can.
        newest_first = list(reversed(numbered))
        if (_running_balance(newest_first, opening)[2] < _running_balance(numbered, opening)[2]):
            numbered = newest_first
            checks.append(Check(
                "row_order", "accounting", Severity.INFO, True,
                "All transactions share one date and the balances show they are listed newest first; "
                "balances were checked from the bottom up.",
            ))

    balance_checks, checked, mismatched = _running_balance(numbered, opening)
    checks += balance_checks
    if checked and not mismatched:
        checks.append(Check(
            "running_balance", "accounting", Severity.ERROR, True,
            f"Running balance is consistent on all {checked} checked transaction(s).",
        ))
    elif checked:
        checks.append(Check(
            "running_balance_summary", "accounting", Severity.INFO, True,
            f"Running balance is consistent on {checked - mismatched} of {checked} checked transaction(s).",
        ))
    elif not checked and rows:
        checks.append(Check(
            "running_balance", "accounting", Severity.WARNING, False,
            "Running balance could not be checked (no printed balances).",
        ))

    if opening is not None and closing is not None:
        debits = [amount(r, "debit") for r in rows]
        credits = [amount(r, "credit") for r in rows]
        if any(d is None and c is None for d, c in zip(debits, credits)):
            checks.append(Check(
                "closing_balance", "accounting", Severity.INFO, True,
                "Closing balance not checked against the transactions: some amounts are missing.",
                fields=["closing_balance"],
            ))
        else:
            calculated = opening - sum((d or ZERO for d in debits), ZERO) + sum((c or ZERO for c in credits), ZERO)
            _compare(
                checks, "closing_balance", calculated=calculated, printed=closing, tolerance=ZERO,
                severity=Severity.ERROR, fields=["opening_balance", "closing_balance"],
                ok="Closing balance = opening balance − total debits + total credits.",
                bad="Closing balance does not equal opening balance − total debits + total credits.",
            )
    last_balance = next((amount(r, "balance") for _, r in reversed(numbered) if amount(r, "balance") is not None), None)
    if closing is not None and last_balance is not None and last_balance != closing:
        checks.append(Check(
            "closing_last_row", "accounting", Severity.ERROR, False,
            "Closing balance differs from the balance after the last transaction.",
            fields=["closing_balance"], calculated=money(last_balance), printed=money(closing),
            difference=money(closing - last_balance),
        ))

    # ---- Identical rows inside the statement (possible double extraction)
    seen: dict[tuple[Any, ...], int] = {}
    for number, row in enumerate(rows, start=1):
        key = (day(row, "txn_date"), amount(row, "debit"), amount(row, "credit"),
               text(row, "reference_number"), normalized_description(text(row, "description")))
        if key[0] is None or (key[1] is None and key[2] is None):
            continue
        if key in seen:
            checks.append(Check(
                "repeated_transaction", "accounting", Severity.WARNING, False,
                f"Same date, amount, reference and description as row {seen[key]}. "
                + ("The running balance moves on both rows, so both are probably real."
                   if not mismatched else "Check whether the row was read twice."),
                row=number, fields=["txn_date", "debit", "credit", "description"],
            ))
        else:
            seen[key] = number
    return checks


def summarize(checks: list[Check]) -> tuple[int, int]:
    return sum(c.counts_as_error for c in checks), sum(c.counts_as_warning for c in checks)
