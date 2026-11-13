"""TallyPrime compatibility checks: a draft against the retrieved masters.

The spec's third validation category (after extraction and accounting). These
checks look at what TallyPrime will do with the voucher: whether every ledger
and the voucher type exist in the company's retrieved masters, whether each
ledger fits its role (a GST ledger for tax, a supplier for the party, …),
whether the GSTIN and the GST type agree with the supplier, whether the date
is inside the books, and whether debits equal credits.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from tally_automation.db.models import TallyGroup, TallyLedger, TallyVoucherType, VoucherKind
from tally_automation.extraction.parsing import account_matches
from tally_automation.validation.accounting import Check, Severity
from tally_automation.vouchers.drafts import ZERO, Draft, InvoicePlan, check, money, rate_key

# GST state codes (first two GSTIN characters) → state / UT names as TallyPrime spells them.
GST_STATES: dict[str, tuple[str, ...]] = {
    "01": ("Jammu & Kashmir",), "02": ("Himachal Pradesh",), "03": ("Punjab",), "04": ("Chandigarh",),
    "05": ("Uttarakhand",), "06": ("Haryana",), "07": ("Delhi",), "08": ("Rajasthan",), "09": ("Uttar Pradesh",),
    "10": ("Bihar",), "11": ("Sikkim",), "12": ("Arunachal Pradesh",), "13": ("Nagaland",), "14": ("Manipur",),
    "15": ("Mizoram",), "16": ("Tripura",), "17": ("Meghalaya",), "18": ("Assam",), "19": ("West Bengal",),
    "20": ("Jharkhand",), "21": ("Odisha", "Orissa"), "22": ("Chhattisgarh",), "23": ("Madhya Pradesh",),
    "24": ("Gujarat",),
    "25": ("Dadra & Nagar Haveli and Daman & Diu", "Daman & Diu"),
    "26": ("Dadra & Nagar Haveli and Daman & Diu", "Dadra & Nagar Haveli"),
    "27": ("Maharashtra",), "28": ("Andhra Pradesh",), "29": ("Karnataka",), "30": ("Goa",), "31": ("Lakshadweep",),
    "32": ("Kerala",), "33": ("Tamil Nadu",), "34": ("Puducherry", "Pondicherry"),
    "35": ("Andaman & Nicobar Islands",), "36": ("Telangana",), "37": ("Andhra Pradesh",), "38": ("Ladakh",),
}

BANK_GROUPS = {"Bank Accounts", "Bank OD A/c", "Bank OCC A/c", "Cash-in-Hand"}
PURCHASE_GROUPS = {"Purchase Accounts", "Direct Expenses", "Indirect Expenses", "Fixed Assets"}
BASE_TYPES = {
    VoucherKind.PURCHASE: "Purchase", VoucherKind.PAYMENT: "Payment",
    VoucherKind.RECEIPT: "Receipt", VoucherKind.CONTRA: "Contra",
}


def normalise_state(name: str | None) -> str:
    return re.sub(r"[^a-z]", "", (name or "").lower().replace("&", "and"))


def same_state(gstin: str | None, state: str | None) -> bool | None:
    """Whether a GSTIN is registered in the named state; None when unknown."""
    if not gstin or not state or gstin[:2] not in GST_STATES:
        return None
    return normalise_state(state) in {normalise_state(n) for n in GST_STATES[gstin[:2]]}


@dataclass
class Masters:
    """One company's retrieved masters, indexed for the checks."""

    company_guid: str
    company_name: str
    company_state: str | None
    books_from: date | None
    ledgers: dict[str, TallyLedger]                      # guid → ledger
    groups: dict[str, TallyGroup]                        # name → group
    voucher_types: dict[str, TallyVoucherType]           # name → voucher type
    _chains: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def chain(self, ledger: TallyLedger) -> tuple[str, ...]:
        """The ledger's group and every group above it."""
        start = ledger.parent
        if start is None:
            return ()
        if start not in self._chains:
            names: list[str] = []
            current: str | None = start
            while current is not None and current not in names and len(names) < 50:
                names.append(current)
                group = self.groups.get(current)
                current = group.parent if group is not None else None
            self._chains[start] = tuple(names)
        return self._chains[start]

    def in_groups(self, ledger: TallyLedger, names: set[str]) -> bool:
        return bool(names.intersection(self.chain(ledger)))

    def is_bank_or_cash(self, ledger: TallyLedger) -> bool:
        return self.in_groups(ledger, BANK_GROUPS)

    def is_duty(self, ledger: TallyLedger) -> bool:
        return ledger.gst_duty_head is not None or self.in_groups(ledger, {"Duties & Taxes"})

    def base_type(self, name: str) -> str | None:
        """A voucher type's predefined base type (Purchase, Payment, …), following its parents."""
        seen: set[str] = set()
        current: str | None = name
        while current is not None and current not in seen:
            seen.add(current)
            vtype = self.voucher_types.get(current)
            if vtype is None:
                return None
            if vtype.parent in (None, current):
                return current
            current = vtype.parent
        return None


def _head(head: str | None) -> str | None:
    if head is None:
        return None
    head = head.upper()
    return "SGST" if head.startswith("SGST") or head.startswith("UTGST") else head


def voucher_type_checks(masters: Masters, name: str, kind: VoucherKind) -> list[Check]:
    vtype = masters.voucher_types.get(name)
    if vtype is None:
        return [check("voucher_type_exists", Severity.ERROR, False,
                      f"Voucher type “{name}” is not in the retrieved masters.")]
    checks = [check("voucher_type_exists", Severity.ERROR, True, f"Voucher type “{name}” exists in TallyPrime.")]
    base = masters.base_type(name)
    wanted = BASE_TYPES[kind]
    checks.append(check(
        "voucher_type_base", Severity.ERROR, base == wanted,
        f"“{name}” is a {wanted} voucher type." if base == wanted
        else f"“{name}” is a {base or 'unknown'} voucher type, not a {wanted} voucher type.",
    ))
    if vtype.is_active is False:
        checks.append(check("voucher_type_active", Severity.ERROR, False, f"Voucher type “{name}” is inactive in TallyPrime."))
    if (vtype.numbering_method or "").lower() == "manual":
        checks.append(check(
            "voucher_numbering", Severity.WARNING, False,
            f"“{name}” uses manual numbering; TallyPrime will import the voucher without a voucher number. "
            "Number it in TallyPrime, or choose a voucher type with automatic numbering.",
        ))
    return checks


def common_checks(masters: Masters, draft: Draft) -> list[Check]:
    checks: list[Check] = []
    missing = [e for e in draft.entries if e.ledger.guid not in masters.ledgers]
    checks.append(check(
        "ledgers_exist", Severity.ERROR, not missing,
        "Every ledger exists in the retrieved masters." if not missing else
        "Not in the retrieved masters: " + ", ".join(f"“{e.ledger.name}”" for e in missing) + ".",
    ))
    renamed = [e for e in draft.entries
               if e.ledger.guid in masters.ledgers and masters.ledgers[e.ledger.guid].name != e.ledger.name]
    if renamed:
        checks.append(check("ledger_names_current", Severity.ERROR, False,
                            "Ledger names changed since they were chosen; generate the voucher again."))
    checks.extend(voucher_type_checks(masters, draft.voucher_type, draft.kind))
    debit, credit = draft.debit_total, draft.credit_total
    checks.append(check(
        "debit_equals_credit", Severity.ERROR, debit == credit and debit > ZERO,
        f"Debits equal credits ({money(debit)})." if debit == credit else
        "Debits do not equal credits.", calculated=money(debit), printed=money(credit), difference=money(debit - credit),
    ))
    if masters.books_from is not None:
        inside = draft.voucher_date >= masters.books_from
        checks.append(check(
            "date_in_books", Severity.ERROR, inside,
            f"The voucher date {draft.voucher_date:%d-%m-%Y} is inside the company's books "
            f"(from {masters.books_from:%d-%m-%Y})." if inside else
            f"The voucher date {draft.voucher_date:%d-%m-%Y} is before the company's books begin "
            f"({masters.books_from:%d-%m-%Y}).",
        ))
    if draft.voucher_date > date.today():
        checks.append(check("date_not_future", Severity.ERROR, False,
                            f"The voucher date {draft.voucher_date:%d-%m-%Y} is in the future."))
    return checks


def purchase_checks(masters: Masters, draft: Draft, plan: InvoicePlan) -> list[Check]:
    checks = common_checks(masters, draft)
    by_role: dict[str, list[TallyLedger]] = {}
    for e in draft.entries:
        ledger = masters.ledgers.get(e.ledger.guid)
        if ledger is not None:
            by_role.setdefault(e.role, []).append(ledger)

    party = (by_role.get("party") or [None])[0]
    if party is not None:
        if party.gst_duty_head or masters.is_bank_or_cash(party) or masters.in_groups(party, PURCHASE_GROUPS):
            checks.append(check("party_ledger_kind", Severity.ERROR, False,
                                f"“{party.name}” is not a party ledger (it is a tax, bank, purchase or expense ledger)."))
        elif not masters.in_groups(party, {"Sundry Creditors"}):
            checks.append(check("party_ledger_group", Severity.WARNING, False,
                                f"“{party.name}” is not under Sundry Creditors (it is under “{party.parent}”)."))
        else:
            checks.append(check("party_ledger_group", Severity.WARNING, True, f"“{party.name}” is a supplier ledger."))
        invoice_gstin = plan.supplier_gstin
        if invoice_gstin and party.gstin:
            agrees = invoice_gstin.upper() == party.gstin.upper()
            checks.append(check(
                "party_gstin_matches", Severity.ERROR, agrees,
                "The supplier ledger's GSTIN matches the invoice." if agrees else
                "The supplier ledger's GSTIN differs from the invoice's GSTIN: this may be the wrong ledger, "
                "or the GSTIN in TallyPrime is out of date.",
                fields=["supplier_gstin"], printed=invoice_gstin, calculated=party.gstin,
            ))
            if party.gstin_valid is False:
                checks.append(check("party_gstin_valid", Severity.WARNING, False,
                                    "The supplier ledger's GSTIN in TallyPrime fails the GSTIN check character."))
        elif invoice_gstin:
            checks.append(check("party_gstin_matches", Severity.WARNING, False,
                                "The supplier ledger has no GSTIN in TallyPrime; GST reports will not show this "
                                "purchase as from a registered supplier.", fields=["supplier_gstin"]))
        else:
            checks.append(check("party_gstin_matches", Severity.INFO, True,
                                "The invoice shows no supplier GSTIN (unregistered supplier).", fields=["supplier_gstin"]))

    roles_by_guid: dict[str, set[str]] = {}
    for e in draft.entries:
        roles_by_guid.setdefault(e.ledger.guid, set()).add(e.role.split(":")[0])
    if any(len(roles) > 1 for roles in roles_by_guid.values()):
        checks.append(check("ledger_roles_distinct", Severity.ERROR, False,
                            "The same ledger is chosen for different roles (party, purchase, tax or round-off)."))

    for ledger in by_role.get("purchase", []):
        if masters.is_duty(ledger) or masters.is_bank_or_cash(ledger) or masters.in_groups(ledger, {"Sundry Creditors", "Sundry Debtors"}):
            checks.append(check("purchase_ledger_kind", Severity.ERROR, False,
                                f"“{ledger.name}” is a tax, bank or party ledger, not a purchase ledger."))
        elif not masters.in_groups(ledger, PURCHASE_GROUPS):
            checks.append(check("purchase_ledger_group", Severity.WARNING, False,
                                f"“{ledger.name}” is not under Purchase Accounts or an expense or fixed-asset group."))

    rates = {rate_key(r) for r in plan.line_rates}
    for e in draft.entries:
        if not e.role.startswith("duty:"):
            continue
        head = e.role.split(":")[1]
        ledger = masters.ledgers.get(e.ledger.guid)
        if ledger is None:
            continue
        if not masters.is_duty(ledger):
            checks.append(check("duty_ledger_kind", Severity.ERROR, False,
                                f"“{ledger.name}” is not a GST duty ledger, but carries the {head} amount."))
            continue
        ledger_head = _head(ledger.gst_duty_head)
        if ledger_head is not None and ledger_head != head:
            checks.append(check("duty_ledger_head", Severity.ERROR, False,
                                f"“{ledger.name}” is a {ledger.gst_duty_head} ledger, but carries the {head} amount."))
        elif ledger_head is None:
            checks.append(check("duty_ledger_head", Severity.WARNING, False,
                                f"“{ledger.name}” has no GST duty head set in TallyPrime; it carries the {head} amount."))
        if ledger.tax_rate and ledger.tax_rate > ZERO and head != "CESS":
            # Per-head rate on the ledger (e.g. 9 for CGST 9%); invoice rates are combined (18 = 9 + 9).
            ledger_rate = ledger.tax_rate * (2 if head in ("CGST", "SGST") else 1)
            if rates and rates != {rate_key(ledger_rate)}:
                checks.append(check(
                    "duty_ledger_rate", Severity.WARNING, False,
                    f"“{ledger.name}” is set up for {rate_key(ledger.tax_rate)}% {head}, but the invoice's line items "
                    f"are at {', '.join(sorted(rates))}% GST.",
                ))

    intra = same_state(plan.supplier_gstin, masters.company_state)
    if intra is not None:
        heads = set(plan.taxes)
        if intra and "IGST" in heads:
            checks.append(check("gst_type_for_state", Severity.WARNING, False,
                                "The supplier is in the company's state, but the invoice charges IGST "
                                "(intra-state supplies normally carry CGST + SGST).", fields=["total_igst"]))
        elif not intra and heads & {"CGST", "SGST"}:
            checks.append(check("gst_type_for_state", Severity.WARNING, False,
                                "The supplier is in another state, but the invoice charges CGST/SGST "
                                "(inter-state supplies normally carry IGST).", fields=["total_cgst", "total_sgst"]))
        elif heads:
            checks.append(check("gst_type_for_state", Severity.WARNING, True,
                                "The GST type fits the supplier's and the company's states."))
    return checks


def bank_checks(masters: Masters, draft: Draft, statement_account: str | None) -> list[Check]:
    checks = common_checks(masters, draft)
    bank_entry = next((e for e in draft.entries if e.role == "bank"), None)
    counter_entry = next((e for e in draft.entries if e.role == "counter"), None)
    bank = masters.ledgers.get(bank_entry.ledger.guid) if bank_entry else None
    counter = masters.ledgers.get(counter_entry.ledger.guid) if counter_entry else None
    if bank is not None:
        if not masters.is_bank_or_cash(bank):
            checks.append(check("bank_ledger_kind", Severity.ERROR, False,
                                f"“{bank.name}” is not under Bank Accounts (or another bank group)."))
        if bank.bank_account_number and statement_account:
            same = account_matches(bank.bank_account_number, statement_account)
            checks.append(check(
                "bank_account_matches", Severity.ERROR, same,
                "The bank ledger's account number matches the statement." if same else
                "The bank ledger's account number differs from the statement's account number.",
            ))
        else:
            checks.append(check("bank_account_matches", Severity.WARNING, False,
                                "The bank ledger's account number could not be compared with the statement "
                                "(one of them is not known)."))
    if bank is not None and counter is not None and bank.guid == counter.guid:
        checks.append(check("ledger_roles_distinct", Severity.ERROR, False,
                            "The transaction's other ledger is the bank ledger itself."))
    if counter is not None and masters.is_duty(counter):
        checks.append(check("counter_ledger_kind", Severity.WARNING, False,
                            f"“{counter.name}” is a tax ledger; payments of tax are usually entered in TallyPrime "
                            "with the tax payment details."))
    if counter is not None and counter.is_bill_wise:
        checks.append(check("bill_on_account", Severity.WARNING, False,
                            f"“{counter.name}” keeps bill-wise details; the amount is recorded On Account. "
                            "Adjust it against the right bills in TallyPrime if needed."))
    return checks


def amount_text(value: Decimal | None) -> str | None:
    return money(value) if value is not None else None
