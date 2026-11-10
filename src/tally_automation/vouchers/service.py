"""Voucher generation: preparing the ledger choice, generating, and checking freshness.

Flow for one document (all on Computer A, nothing sent to TallyPrime):

1. ``prepare`` – the document's eligibility, what its voucher(s) need (purchase
   amount split, tax heads, round-off, or the bank transactions) and ledger
   *suggestions*: deterministic matches (same GSTIN, same bank account number,
   matching GST duty head) and the ledgers chosen last time for the same key.
   No AI is involved, and a suggestion is never used unless a person submits it.
2. ``generate`` – with the ledgers the reviewer submitted: builds each voucher,
   renders its import XML, runs the TallyPrime compatibility and XML checks,
   and stores it (``ready`` or ``blocked``). A previous voucher for the same
   source is superseded. The chosen ledgers are remembered as future
   suggestions.
3. ``stale_reasons`` – whether a stored voucher still reflects the document and
   the masters (edits, re-extraction, masters retrieved again, eligibility).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from tally_automation.db.base import utc_now
from tally_automation.db.models import (
    ApprovalDecision, ApprovalStatus, ImportStatus, VoucherApproval,
    BankStatement, BankTransaction, Document, DocumentType, ExtractionRun, GeneratedVoucher, Invoice,
    MappingMemory, MappingPurpose, TallyCompany, TallyGroup, TallyLedger, TallyVoucherType, VoucherKind,
    VoucherSource, VoucherStatus,
)
from tally_automation.validation.accounting import Check, Severity, summarize
from tally_automation.validation.service import Eligibility, current_run_ids, eligibility
from tally_automation.vouchers import matching
from tally_automation.vouchers.compat import BASE_TYPES, Masters, bank_checks, purchase_checks
from tally_automation.vouchers.drafts import (
    HEADS, BankLedgers, Draft, InvoicePlan, Ledger, PurchaseLedgers, TransactionPlan, bank_voucher_kind,
    build_bank, build_purchase, check, plan_invoice, plan_transaction, rate_key,
)
from tally_automation.vouchers.xml import render_xml, verify_xml

RULES_VERSION = "2026-11-06.1"
logger = logging.getLogger(__name__)


class VoucherError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---- Masters --------------------------------------------------------------------------------

def load_masters(session: Session, company_guid: str) -> tuple[TallyCompany, Masters]:
    company = session.scalar(select(TallyCompany).where(TallyCompany.guid == company_guid))
    if company is None or company.last_sync_run_id is None:
        raise VoucherError("no_masters", "No retrieved TallyPrime masters for this company. Retrieve them on the TallyPrime page.")
    masters = Masters(
        company_guid=company.guid, company_name=company.name, company_state=company.state,
        books_from=company.books_from,
        ledgers={l.guid: l for l in session.scalars(select(TallyLedger).where(TallyLedger.company_guid == company_guid))},
        groups={g.name: g for g in session.scalars(select(TallyGroup).where(TallyGroup.company_guid == company_guid))},
        voucher_types={v.name: v for v in session.scalars(
            select(TallyVoucherType).where(TallyVoucherType.company_guid == company_guid))},
    )
    return company, masters


def _ledger(masters: Masters, guid: str | None, role: str) -> Ledger:
    record = masters.ledgers.get(guid or "")
    if record is None:
        raise VoucherError("unknown_ledger", f"The ledger chosen for {role} is not in the retrieved masters. "
                                             "Reload the page and choose again.")
    return Ledger(record.guid, record.name, bool(record.is_bill_wise), masters.is_bank_or_cash(record))


# ---- Source records ---------------------------------------------------------------------------

@dataclass
class Source:
    document: Document
    run: ExtractionRun
    invoice: Invoice | None = None
    statement: BankStatement | None = None


def _source(session: Session, document_id: int) -> Source:
    document = session.get(Document, document_id)
    if document is None:
        raise VoucherError("not_found", "Document not found.")
    run_id = current_run_ids(session, [document_id]).get(document_id)
    if run_id is None:
        raise VoucherError("no_extraction", "This document has no successful extraction.")
    run = session.get(ExtractionRun, run_id)
    assert run is not None
    if run.document_type is DocumentType.PURCHASE_INVOICE:
        invoice = session.scalar(select(Invoice).options(selectinload(Invoice.line_items))
                                 .where(Invoice.extraction_run_id == run.id))
        return Source(document, run, invoice=invoice)
    statement = session.scalar(select(BankStatement).options(selectinload(BankStatement.transactions))
                               .where(BankStatement.extraction_run_id == run.id))
    return Source(document, run, statement=statement)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def invoice_digest(invoice: Invoice) -> str:
    return digest([invoice.fields, [item.fields for item in invoice.line_items]])


def transaction_digest(statement: BankStatement, transaction: BankTransaction) -> str:
    return digest([transaction.fields, statement.account_number])


def marker(document: Document, suffix: str) -> str:
    """Stable per source: the document file's hash, not a database id, so it survives a database rebuild."""
    return f"TA-{document.file.sha256[:10].upper()}-{suffix}"


def description_key(text: str | None) -> str | None:
    """A transaction description without numbers (UTRs, dates, amounts), for remembering the other ledger."""
    if not text:
        return None
    words = re.sub(r"[^a-z]+", " ", text.lower()).split()
    key = " ".join(w for w in words if len(w) > 1)[:128]
    return key or None


def category_key(kind: str) -> str:
    """Memory key shared by every narration of one kind (bank charges, TDS, cash), whose
    exact wording changes with each reference number."""
    return f"category:{kind}"


def override_key(description_key: str | None) -> str | None:
    """Memory key for a ledger the user chose *instead of* the party named in the narration."""
    return f"override:{description_key}"[:128] if description_key else None


def _name_key(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower().replace("&", "and"))


# ---- Suggestions --------------------------------------------------------------------------------

@dataclass
class Suggestion:
    guid: str | None
    name: str
    reason: str
    parent: str | None = None
    gstin: str | None = None

    def to_json(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _remembered(session: Session, company_guid: str, purpose: MappingPurpose, key: str | None) -> MappingMemory | None:
    if key is None:
        return None
    return session.scalar(select(MappingMemory).where(
        MappingMemory.company_guid == company_guid, MappingMemory.purpose == purpose, MappingMemory.key == key[:128]))


def _suggest(ledger: TallyLedger, reason: str) -> Suggestion:
    return Suggestion(ledger.guid, ledger.name, reason, ledger.parent, ledger.gstin)


def _dedupe(items: list[Suggestion]) -> list[Suggestion]:
    seen: set[str | None] = set()
    out = []
    for s in items:
        if s.guid not in seen:
            seen.add(s.guid)
            out.append(s)
    return out[:10]


def _memory_suggestion(session: Session, masters: Masters, purpose: MappingPurpose, key: str | None) -> list[Suggestion]:
    memory = _remembered(session, masters.company_guid, purpose, key)
    if memory is None:
        return []
    ledger = masters.ledgers.get(memory.ledger_guid or "")
    if ledger is None:
        # A ledger created by an import since (remembered by its stand-in GUID), or re-created in
        # TallyPrime: found again by its name.
        wanted = " ".join((memory.name or "").lower().split())
        ledger = next((l for l in masters.ledgers.values() if " ".join(l.name.lower().split()) == wanted), None)
    return [_suggest(ledger, "Chosen last time")] if ledger is not None else []


def _voucher_type_options(session: Session, masters: Masters, kind: VoucherKind) -> list[Suggestion]:
    base = BASE_TYPES[kind]
    names = sorted(
        (v.name for v in masters.voucher_types.values() if v.is_active is not False and masters.base_type(v.name) == base),
        key=lambda n: (n != base, n),
    )
    memory = _remembered(session, masters.company_guid, MappingPurpose.VOUCHER_TYPE, kind.value)
    if memory is not None and memory.name in names:
        names.remove(memory.name)
        names.insert(0, memory.name)
    return [Suggestion(None, n, "Chosen last time" if memory is not None and n == memory.name
                       else "Predefined voucher type" if n == base else f"{base} voucher type") for n in names]


def _party_key(plan: InvoicePlan) -> str | None:
    if plan.supplier_gstin:
        return plan.supplier_gstin.upper()
    return f"name:{_name_key(plan.supplier_name)}" if plan.supplier_name else None


def _invoice_suggestions(session: Session, masters: Masters, plan: InvoicePlan) -> dict[str, Any]:
    ledgers = list(masters.ledgers.values())
    party = _memory_suggestion(session, masters, MappingPurpose.PARTY, _party_key(plan))
    if plan.supplier_gstin:
        party += [_suggest(l, "Same GSTIN as the invoice") for l in ledgers
                  if l.gstin and l.gstin.upper() == plan.supplier_gstin.upper() and not masters.is_duty(l)]
    if plan.supplier_name:
        wanted = _name_key(plan.supplier_name)
        party += [_suggest(l, "Same name as the supplier") for l in ledgers
                  if wanted and _name_key(l.name) == wanted and not masters.is_duty(l)]

    purchase: dict[str, list[Suggestion]] = {}
    purchase_ledgers = [l for l in ledgers if masters.in_groups(l, {"Purchase Accounts"})]
    for bucket in plan.buckets:
        options = _memory_suggestion(session, masters, MappingPurpose.PURCHASE, bucket.key)
        if bucket.key != "all":
            options += [_suggest(l, f"Name mentions {bucket.key}%") for l in purchase_ledgers
                        if re.search(rf"(?<![\d.]){re.escape(bucket.key)}\s*%", l.name)]
        if len(purchase_ledgers) == 1:
            options.append(_suggest(purchase_ledgers[0], "The only purchase ledger"))
        purchase[bucket.key] = _dedupe(options)

    rates = {rate_key(r) for r in plan.line_rates}
    duty: dict[str, list[Suggestion]] = {}
    for head in plan.taxes:
        options = _memory_suggestion(session, masters, MappingPurpose.DUTY, head)
        matching = [l for l in ledgers if l.gst_duty_head and
                    (l.gst_duty_head.upper().startswith(head) or (head == "SGST" and "UTGST" in l.gst_duty_head.upper()))]
        if head != "CESS" and len(rates) == 1:
            factor = 2 if head in ("CGST", "SGST") else 1
            exact = [l for l in matching if l.tax_rate and rate_key(l.tax_rate * factor) in rates]
            options += [_suggest(l, f"{head} ledger for {next(iter(rates))}% GST") for l in exact]
        options += [_suggest(l, f"{head} ledger") for l in matching]
        duty[head] = _dedupe(options)

    round_off: list[Suggestion] = []
    if plan.round_off:
        round_off = _memory_suggestion(session, masters, MappingPurpose.ROUND_OFF, "round_off")
        round_off += [_suggest(l, "Name mentions rounding") for l in ledgers if "round" in l.name.lower()]
    return {
        "party": [s.to_json() for s in _dedupe(party)],
        "purchase": {k: [s.to_json() for s in v] for k, v in purchase.items()},
        "duty": {k: [s.to_json() for s in v] for k, v in duty.items()},
        "round_off": [s.to_json() for s in _dedupe(round_off)],
        "voucher_types": {VoucherKind.PURCHASE.value: [s.to_json() for s in _voucher_type_options(session, masters, VoucherKind.PURCHASE)]},
    }


def _statement_suggestions(session: Session, masters: Masters, statement: BankStatement,
                           plans: list[TransactionPlan]) -> dict[str, Any]:
    account = re.sub(r"\D", "", statement.account_number or "")
    bank = _memory_suggestion(session, masters, MappingPurpose.BANK, account or None)
    ifsc = (statement.fields.get("ifsc") or {}).get("value")
    bank += [_suggest(c.ledger, c.reason) for c in matching.statement_bank(masters, statement.account_number, ifsc)]
    bank = _dedupe(bank)
    bank_guids = {s.guid for s in bank[:1] if s.guid}
    counter: dict[str, list[dict[str, Any]]] = {}
    for p in plans:
        key = description_key(p.description)
        options: list[Suggestion] = []
        fixed = matching.fixed_category(p.description)
        if fixed is not None:
            # Narrations the user has fixed to one kind ("TDS CASH WD" is always bank charges, a loan-account
            # transfer always the housing loan): that kind's ledger comes first, before anything remembered
            # for this narration (earlier vouchers may have used another ledger).
            label = matching.CATEGORY_LABELS[fixed]
            options += [
                Suggestion(s.guid, s.name, f"Chosen last time for {label}", s.parent, s.gstin)
                for s in _memory_suggestion(session, masters, MappingPurpose.COUNTER, category_key(fixed))
            ]
            options += [_suggest(c.ledger, c.reason)
                        for c in matching.counter_ledgers(masters, p.description, p.money_out, bank_guids)]
        # A ledger the user picked over the party named in the narration wins; then the named party;
        # then what was simply used last time (which may only have been an accepted pre-fill).
        options += _memory_suggestion(session, masters, MappingPurpose.COUNTER, override_key(key))
        named = matching.party_in_narration(masters, p.description, p.money_out, bank_guids)
        if named is not None:
            options.append(_suggest(named.ledger, named.reason))
        options += _memory_suggestion(session, masters, MappingPurpose.COUNTER, key)
        kind = matching.category(p.description)
        if kind is not None:
            options += [
                Suggestion(s.guid, s.name, f"Chosen last time for {matching.CATEGORY_LABELS[kind]}", s.parent, s.gstin)
                for s in _memory_suggestion(session, masters, MappingPurpose.COUNTER, category_key(kind))
            ]
        options += [_suggest(c.ledger, c.reason)
                    for c in matching.counter_ledgers(masters, p.description, p.money_out, bank_guids)]
        counter[str(p.transaction_id)] = [s.to_json() for s in _dedupe(options)]
    return {
        "bank": [s.to_json() for s in bank],
        "counter": counter,
        "ledger_groups": creatable_groups(masters),
        "voucher_types": {kind.value: [s.to_json() for s in _voucher_type_options(session, masters, kind)]
                          for kind in (VoucherKind.PAYMENT, VoucherKind.RECEIPT, VoucherKind.CONTRA)},
    }


# ---- Prepare ------------------------------------------------------------------------------------------

def _choose_company(session: Session, company_guid: str | None) -> TallyCompany | None:
    companies = session.scalars(select(TallyCompany).where(TallyCompany.last_sync_run_id.is_not(None))
                                .order_by(TallyCompany.last_synced_at.desc())).all()
    if company_guid:
        company = next((c for c in companies if c.guid == company_guid), None)
        if company is None:
            raise VoucherError("no_masters", "No retrieved TallyPrime masters for this company.")
        return company
    return companies[0] if companies else None


def prepare(session: Session, document_id: int, company_guid: str | None = None) -> dict[str, Any]:
    source = _source(session, document_id)
    e = eligibility(session, document_id)
    company = _choose_company(session, company_guid)
    out: dict[str, Any] = {
        "document_id": document_id, "document_type": source.run.document_type.value,
        "original_filename": source.document.original_filename,
        "eligibility": {"eligible": e.eligible, "reasons": e.reasons, "excluded_transaction_ids": e.excluded_transaction_ids},
        "company": None if company is None else {
            "guid": company.guid, "name": company.name, "state": company.state, "books_from": company.books_from,
            "last_synced_at": company.last_synced_at,
        },
        "invoice": None, "statement": None, "suggestions": None,
    }
    masters = load_masters(session, company.guid)[1] if company is not None else None
    if source.invoice is not None:
        plan = plan_invoice(source.invoice.fields, [i.fields for i in source.invoice.line_items])
        out["invoice"] = {
            "invoice_id": source.invoice.id, "invoice_number": plan.invoice_number, "invoice_date": plan.invoice_date,
            "supplier_name": plan.supplier_name, "supplier_gstin": plan.supplier_gstin,
            "grand_total": plan.grand_total, "taxable": plan.taxable, "round_off": plan.round_off,
            "buckets": [{"key": b.key, "label": b.label, "taxable": b.taxable} for b in plan.buckets],
            "taxes": [{"head": h, "amount": plan.taxes[h]} for h in HEADS if h in plan.taxes],
            "checks": [c.to_json() for c in plan.checks],
        }
        if masters is not None:
            out["suggestions"] = _invoice_suggestions(session, masters, plan)
    elif source.statement is not None:
        statement = source.statement
        plans = [plan_transaction(t.id, t.sequence, t.fields) for t in statement.transactions]
        out["statement"] = {
            "statement_id": statement.id, "account_number": statement.account_number,   # masked by the API
            "transactions": [{
                "id": p.transaction_id, "sequence": p.sequence, "date": p.txn_date, "money_out": p.money_out,
                "amount": p.amount, "description": p.description, "reference": p.reference,
                "excluded": p.transaction_id in e.excluded_transaction_ids,
                "checks": [c.to_json() for c in p.checks],
            } for p in plans],
        }
        if masters is not None:
            out["suggestions"] = _statement_suggestions(session, masters, statement, plans)
    return out


# ---- Generate ---------------------------------------------------------------------------------------------

@dataclass
class InvoiceChoice:
    voucher_type: str
    party: str
    purchase: dict[str, str]
    duty: dict[str, str] = field(default_factory=dict)
    round_off: str | None = None


@dataclass
class StatementChoice:
    bank: str
    voucher_types: dict[str, str]              # payment / receipt / contra → voucher type name
    transactions: dict[int, str]               # transaction id → other ledger's GUID
    # transaction id → a ledger that is not in TallyPrime yet: created there when the voucher is imported
    new_ledgers: dict[int, NewLedger] = field(default_factory=dict)


@dataclass
class NewLedger:
    name: str
    group: str


NEW_LEDGER_PREFIX = "new:"
NEW_LEDGER_MAX_NAME = 100
# A new ledger may not be a bank, cash or tax ledger: those need details (account numbers, tax heads)
# that are set up in TallyPrime itself.
_NO_NEW_LEDGERS_UNDER = {"Bank Accounts", "Bank OD A/c", "Bank OCC A/c", "Cash-in-Hand", "Duties & Taxes"}


def new_ledger_guid(name: str) -> str:
    """A stand-in GUID for a ledger that does not exist yet (stable for the same name)."""
    return NEW_LEDGER_PREFIX + hashlib.sha256(" ".join(name.lower().split()).encode()).hexdigest()[:24]


def creatable_groups(masters: Masters) -> list[str]:
    """Groups a new ledger may be created under."""
    out = []
    for name in sorted(masters.groups, key=str.lower):
        stand_in = TallyLedger(name="", parent=name)
        if not masters.in_groups(stand_in, _NO_NEW_LEDGERS_UNDER):
            out.append(name)
    return out


def _with_new_ledgers(masters: Masters, choice: StatementChoice) -> tuple[Masters, dict[str, dict[str, str]]]:
    """Masters with stand-ins for the new ledgers, and {stand-in guid: {name, parent}}. A name that already
    exists in TallyPrime (any case) uses that ledger instead; nothing is created twice."""
    if not choice.new_ledgers:
        return masters, {}
    existing = {" ".join(l.name.lower().split()): l for l in masters.ledgers.values()}
    allowed = set(creatable_groups(masters))
    extra: dict[str, TallyLedger] = {}
    info: dict[str, dict[str, str]] = {}
    for txn_id, new in choice.new_ledgers.items():
        name = " ".join(new.name.split())
        if not name or len(name) > NEW_LEDGER_MAX_NAME or re.search(r"[\x00-\x1f\x7f<>]", name):
            raise VoucherError("invalid_new_ledger", f"“{name[:40]}” cannot be used as a ledger name "
                                                     f"(1–{NEW_LEDGER_MAX_NAME} characters, no < or >).")
        same = existing.get(name.lower())
        if same is not None:
            choice.transactions[txn_id] = same.guid
            continue
        if new.group not in allowed:
            raise VoucherError("invalid_new_ledger", f"A new ledger cannot be created under “{new.group}”. "
                                                     "Choose another group, or create it in TallyPrime.")
        guid = new_ledger_guid(name)
        if guid in info:
            # The same new ledger for another transaction: one ledger, spelled as first typed.
            if info[guid]["parent"] != new.group:
                raise VoucherError("invalid_new_ledger", f"The new ledger “{name}” is given two different groups.")
            choice.transactions[txn_id] = guid
            continue
        extra[guid] = TallyLedger(company_guid=masters.company_guid, guid=guid, name=name, aliases=[],
                                  parent=new.group, is_bill_wise=False)
        info[guid] = {"name": name, "parent": new.group}
        choice.transactions[txn_id] = guid
    copy = Masters(masters.company_guid, masters.company_name, masters.company_state, masters.books_from,
                   {**masters.ledgers, **extra}, masters.groups, masters.voucher_types)
    return copy, info


def _forget(session: Session, company_guid: str, purpose: MappingPurpose, key: str | None) -> None:
    memory = _remembered(session, company_guid, purpose, key)
    if memory is not None:
        session.delete(memory)
        session.flush()


def _remember(session: Session, company_guid: str, purpose: MappingPurpose, key: str | None,
              guid: str | None, name: str) -> None:
    if not key:
        return
    memory = _remembered(session, company_guid, purpose, key)
    if memory is None:
        session.add(MappingMemory(company_guid=company_guid, purpose=purpose, key=key[:128], ledger_guid=guid, name=name))
        session.flush()   # the session does not autoflush; the next lookup must see this row
    else:
        memory.ledger_guid, memory.name, memory.use_count = guid, name, memory.use_count + 1


def _supersede(session: Session, source_kind: VoucherSource, source_id: int) -> None:
    current = session.scalars(select(GeneratedVoucher).where(
        GeneratedVoucher.source_kind == source_kind, GeneratedVoucher.source_id == source_id,
        GeneratedVoucher.status != VoucherStatus.SUPERSEDED,
    )).all()
    for voucher in current:
        if voucher.import_status in (ImportStatus.IMPORTING, ImportStatus.IMPORTED, ImportStatus.UNCERTAIN):
            raise VoucherError(
                "already_imported",
                "A voucher for this record is already in TallyPrime (or its import has no clear answer yet); "
                "it cannot be generated again.",
            )
        if voucher.approval_status is ApprovalStatus.APPROVED:
            voucher.approval_status, voucher.approved_xml_sha256 = ApprovalStatus.PENDING, None
            session.add(VoucherApproval(
                voucher_id=voucher.id, decision=ApprovalDecision.REVOKED, xml_sha256=voucher.xml_sha256,
                warnings_acknowledged=0, note="Replaced by a new generation.", decided_at=utc_now(),
            ))
        if voucher.import_status is ImportStatus.FAILED:
            voucher.import_status = ImportStatus.NOT_IMPORTED
        voucher.status, voucher.superseded_at = VoucherStatus.SUPERSEDED, utc_now()
    session.flush()


def _finish(draft: Draft | None, company_name: str, checks: list[Check]) -> tuple[str | None, list[Check]]:
    if draft is None:
        return None, checks
    xml = render_xml(draft, company_name)
    problems = verify_xml(xml, draft, company_name)
    checks = checks + (problems or [check(
        "xml_structure", Severity.ERROR, True,
        "The import XML is well-formed, contains exactly this voucher, and its entries balance.",
    )])
    return xml, checks


def _store(session: Session, *, source: Source, source_kind: VoucherSource, source_id: int, source_digest: str,
           company: TallyCompany, kind: VoucherKind, voucher_type: str | None, draft: Draft | None,
           xml: str | None, checks: list[Check], mapping: dict[str, Any], marker_text: str,
           client_address: str | None, user_id: int | None = None) -> GeneratedVoucher:
    errors, warnings = summarize(checks)
    ready = errors == 0 and xml is not None
    _supersede(session, source_kind, source_id)
    voucher = GeneratedVoucher(
        document_id=source.document.id, extraction_run_id=source.run.id, source_kind=source_kind,
        source_id=source_id, source_digest=source_digest, company_guid=company.guid, company_name=company.name,
        masters_sync_run_id=company.last_sync_run_id, voucher_kind=kind, voucher_type_name=voucher_type,
        voucher_date=draft.voucher_date if draft else None, reference=draft.reference if draft else None,
        party_ledger_name=draft.party.name if draft and draft.party else None,
        amount=draft.amount if draft else None, marker=marker_text,
        entries=[e.to_json() for e in draft.entries] if draft else [], mapping=mapping,
        narration=draft.narration if draft else None, xml=xml,
        xml_sha256=hashlib.sha256(xml.encode()).hexdigest() if xml else None,
        status=VoucherStatus.READY if ready else VoucherStatus.BLOCKED,
        error_count=errors, warning_count=warnings, checks=[c.to_json() for c in checks],
        rules_version=RULES_VERSION, client_address=client_address, generated_by_user_id=user_id,
    )
    session.add(voucher)
    session.flush()
    return voucher


def _require_eligible(e: Eligibility) -> None:
    if not e.eligible:
        raise VoucherError("not_eligible", "The document is not eligible for voucher generation: " + " ".join(e.reasons))


def generate_invoice(session: Session, document_id: int, company_guid: str, choice: InvoiceChoice,
                     *, client_address: str | None, user_id: int | None = None) -> list[GeneratedVoucher]:
    source = _source(session, document_id)
    if source.invoice is None:
        raise VoucherError("wrong_type", "This document is not a purchase invoice.")
    _require_eligible(eligibility(session, document_id))
    company, masters = load_masters(session, company_guid)
    invoice = source.invoice
    plan = plan_invoice(invoice.fields, [i.fields for i in invoice.line_items])

    missing = [b.label for b in plan.buckets if b.key not in choice.purchase]
    missing += [f"{h} ledger" for h in plan.taxes if h not in choice.duty]
    if plan.round_off and not choice.round_off:
        missing.append("round-off ledger")
    if missing:
        raise VoucherError("mapping_incomplete", "Choose a ledger for: " + ", ".join(missing) + ".")
    ledgers = PurchaseLedgers(
        voucher_type=choice.voucher_type,
        party=_ledger(masters, choice.party, "the supplier"),
        purchase={b.key: _ledger(masters, choice.purchase[b.key], b.label) for b in plan.buckets},
        duty={h: _ledger(masters, choice.duty[h], h) for h in plan.taxes},
        round_off=_ledger(masters, choice.round_off, "round-off") if plan.round_off else None,
    )
    mark = marker(source.document, "INV")
    draft = None
    checks = list(plan.checks)
    if plan.buildable:
        draft = build_purchase(plan, ledgers, marker=mark, document_id=document_id)
        party = masters.ledgers[ledgers.party.guid]
        draft.party_gstin = party.gstin if party.gstin_valid else None
        checks += purchase_checks(masters, draft, plan)
    xml, checks = _finish(draft, company.name, checks)
    mapping = {
        "voucher_type": choice.voucher_type, "party": ledgers.party.guid,
        "purchase": {k: v.guid for k, v in ledgers.purchase.items()},
        "duty": {k: v.guid for k, v in ledgers.duty.items()},
        "round_off": ledgers.round_off.guid if ledgers.round_off else None,
    }
    voucher = _store(
        session, source=source, source_kind=VoucherSource.INVOICE, source_id=invoice.id,
        source_digest=invoice_digest(invoice), company=company, kind=VoucherKind.PURCHASE,
        voucher_type=choice.voucher_type, draft=draft, xml=xml, checks=checks, mapping=mapping,
        marker_text=mark, client_address=client_address, user_id=user_id,
    )
    _remember(session, company.guid, MappingPurpose.PARTY, _party_key(plan), ledgers.party.guid, ledgers.party.name)
    for key, ledger in ledgers.purchase.items():
        _remember(session, company.guid, MappingPurpose.PURCHASE, key, ledger.guid, ledger.name)
    for head, ledger in ledgers.duty.items():
        _remember(session, company.guid, MappingPurpose.DUTY, head, ledger.guid, ledger.name)
    if ledgers.round_off:
        _remember(session, company.guid, MappingPurpose.ROUND_OFF, "round_off", ledgers.round_off.guid, ledgers.round_off.name)
    _remember(session, company.guid, MappingPurpose.VOUCHER_TYPE, VoucherKind.PURCHASE.value, None, choice.voucher_type)
    logger.info("vouchers generated", extra={"document_id": document_id, "count": 1,
                                              "ready": int(voucher.status is VoucherStatus.READY)})
    return [voucher]


def generate_statement(session: Session, document_id: int, company_guid: str, choice: StatementChoice,
                       *, client_address: str | None, user_id: int | None = None) -> list[GeneratedVoucher]:
    source = _source(session, document_id)
    if source.statement is None:
        raise VoucherError("wrong_type", "This document is not a bank statement.")
    e = eligibility(session, document_id)
    _require_eligible(e)
    if not choice.transactions and not choice.new_ledgers:
        raise VoucherError("mapping_incomplete", "Choose the other ledger for at least one transaction.")
    company, masters = load_masters(session, company_guid)
    statement = source.statement
    by_id = {t.id: t for t in statement.transactions}
    masters, new_ledgers = _with_new_ledgers(masters, choice)
    unknown = [i for i in choice.transactions if i not in by_id]
    if unknown:
        raise VoucherError("unknown_transaction", "A chosen transaction does not belong to this statement's current extraction.")
    excluded = [by_id[i].sequence for i in choice.transactions if i in e.excluded_transaction_ids]
    if excluded:
        raise VoucherError("excluded_transaction",
                           f"Transaction(s) {', '.join(map(str, excluded))} are confirmed duplicates and get no voucher.")
    bank = _ledger(masters, choice.bank, "the bank account")

    vouchers: list[GeneratedVoucher] = []
    for txn_id, counter_guid in sorted(choice.transactions.items(), key=lambda item: by_id[item[0]].sequence):
        transaction = by_id[txn_id]
        plan = plan_transaction(transaction.id, transaction.sequence, transaction.fields)
        counter = _ledger(masters, counter_guid, f"transaction {transaction.sequence}")
        kind = bank_voucher_kind(plan, counter) if plan.money_out is not None else (
            VoucherKind.PAYMENT if transaction.debit else VoucherKind.RECEIPT)
        voucher_type = choice.voucher_types.get(kind.value)
        if not voucher_type:
            raise VoucherError("mapping_incomplete", f"Choose a {BASE_TYPES[kind]} voucher type.")
        mark = marker(source.document, f"T{transaction.sequence}")
        draft = None
        checks = list(plan.checks)
        if plan.buildable:
            draft = build_bank(plan, BankLedgers(bank, counter, voucher_type, kind), marker=mark, document_id=document_id)
            checks += bank_checks(masters, draft, statement.account_number)
        created = new_ledgers.get(counter.guid)
        if created is not None:
            checks.append(check("new_ledger", Severity.WARNING, False,
                                f"“{created['name']}” is a new ledger: it will be created in TallyPrime under "
                                f"“{created['parent']}” when this voucher is imported."))
        xml, checks = _finish(draft, company.name, checks)
        mapping: dict[str, Any] = {"bank": bank.guid, "counter": counter.guid, "voucher_type": voucher_type}
        if created is not None:
            mapping["new_ledgers"] = [created]
        vouchers.append(_store(
            session, source=source, source_kind=VoucherSource.BANK_TRANSACTION, source_id=transaction.id,
            source_digest=transaction_digest(statement, transaction), company=company, kind=kind,
            voucher_type=voucher_type, draft=draft, xml=xml, checks=checks,
            mapping=mapping,
            marker_text=mark, client_address=client_address, user_id=user_id,
        ))
        key = description_key(plan.description)
        _remember(session, company.guid, MappingPurpose.COUNTER, key, counter.guid, counter.name)
        named = matching.party_in_narration(masters, plan.description, plan.money_out, {bank.guid})
        if named is not None and named.ledger.guid != counter.guid:
            _remember(session, company.guid, MappingPurpose.COUNTER, override_key(key), counter.guid, counter.name)
        elif named is not None:
            _forget(session, company.guid, MappingPurpose.COUNTER, override_key(key))
        narration_kind = matching.category(plan.description)
        if narration_kind is not None:
            _remember(session, company.guid, MappingPurpose.COUNTER, category_key(narration_kind), counter.guid, counter.name)
        _remember(session, company.guid, MappingPurpose.VOUCHER_TYPE, kind.value, None, voucher_type)
    account = re.sub(r"\D", "", statement.account_number or "")
    _remember(session, company.guid, MappingPurpose.BANK, account or None, bank.guid, bank.name)
    logger.info("vouchers generated", extra={
        "document_id": document_id, "count": len(vouchers),
        "ready": sum(v.status is VoucherStatus.READY for v in vouchers),
    })
    return vouchers


# ---- Freshness ----------------------------------------------------------------------------------------------

def stale_reasons(session: Session, voucher: GeneratedVoucher, *, cache: dict[str, Any] | None = None) -> list[str]:
    """Why a stored voucher no longer reflects its document or the masters (empty: it is current)."""
    if voucher.status is VoucherStatus.SUPERSEDED:
        return ["Replaced by a later generation."]
    cache = {} if cache is None else cache
    reasons: list[str] = []
    document_id = voucher.document_id
    if ("run", document_id) not in cache:
        cache[("run", document_id)] = current_run_ids(session, [document_id]).get(document_id)
        cache[("eligibility", document_id)] = eligibility(session, document_id)
    if cache[("run", document_id)] != voucher.extraction_run_id:
        return ["The document was extracted again since; generate the voucher again."]
    e: Eligibility = cache[("eligibility", document_id)]
    if not e.eligible:
        reasons.append("The document is no longer eligible: " + " ".join(e.reasons))
    if voucher.source_kind is VoucherSource.INVOICE:
        invoice = session.scalar(select(Invoice).options(selectinload(Invoice.line_items)).where(Invoice.id == voucher.source_id))
        current = invoice_digest(invoice) if invoice else None
    else:
        transaction = session.get(BankTransaction, voucher.source_id)
        current = transaction_digest(transaction.statement, transaction) if transaction else None
        if voucher.source_id in e.excluded_transaction_ids:
            reasons.append("The transaction has since been confirmed as a duplicate.")
    if current != voucher.source_digest:
        reasons.append("The extracted data was edited since; generate the voucher again.")
    company = session.scalar(select(TallyCompany).where(TallyCompany.guid == voucher.company_guid))
    if company is None or company.last_sync_run_id != voucher.masters_sync_run_id:
        reasons.append("TallyPrime masters were retrieved again since; generate again to re-check the ledgers.")
    return reasons


def document_vouchers(session: Session, document_id: int, *, include_superseded: bool = False) -> list[GeneratedVoucher]:
    query = select(GeneratedVoucher).where(GeneratedVoucher.document_id == document_id)
    if not include_superseded:
        query = query.where(GeneratedVoucher.status != VoucherStatus.SUPERSEDED)
    return list(session.scalars(query.order_by(GeneratedVoucher.source_kind, GeneratedVoucher.source_id,
                                               GeneratedVoucher.generated_at.desc())))
