"""Persisting extraction results and applying audited reviewer edits."""

from __future__ import annotations

import copy
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from tally_automation.db.base import utc_now
from tally_automation.db.models import (
    BankStatement, BankTransaction, Document, DocumentType, EditableEntity, ExtractionRun, FieldEdit,
    Invoice, InvoiceLineItem,
)
from tally_automation.extraction.parsing import Status, parse_value
from tally_automation.extraction.schemas import (
    INVOICE_FIELDS, LINE_ITEM_FIELDS, STATEMENT_FIELDS, TRANSACTION_FIELDS, FieldSpec,
)
from tally_automation.extraction.service import REQUIRED_FIELDS, ExtractionResult

ENTITY_FIELDS: dict[EditableEntity, tuple[FieldSpec, ...]] = {
    EditableEntity.INVOICE: INVOICE_FIELDS,
    EditableEntity.INVOICE_LINE_ITEM: LINE_ITEM_FIELDS,
    EditableEntity.BANK_STATEMENT: STATEMENT_FIELDS,
    EditableEntity.BANK_TRANSACTION: TRANSACTION_FIELDS,
}
ENTITY_MODELS = {
    EditableEntity.INVOICE: Invoice,
    EditableEntity.INVOICE_LINE_ITEM: InvoiceLineItem,
    EditableEntity.BANK_STATEMENT: BankStatement,
    EditableEntity.BANK_TRANSACTION: BankTransaction,
}
_USABLE = {Status.VALID.value, Status.WARNING.value}

Entity = Invoice | InvoiceLineItem | BankStatement | BankTransaction


class EditError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---- Key columns -----------------------------------------------------------------

def _usable(fields: dict[str, dict[str, Any]], name: str) -> str | None:
    meta = fields.get(name) or {}
    return meta.get("value") if meta.get("status") in _USABLE else None


def _as_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _as_decimal(value: str | None) -> Decimal | None:
    return Decimal(value) if value else None


def sync_key_columns(entity: Entity) -> None:
    """Mirror usable field values into the typed, indexed columns."""
    f = entity.fields
    if isinstance(entity, Invoice):
        entity.supplier_gstin = _usable(f, "supplier_gstin")
        number = _usable(f, "invoice_number")
        entity.invoice_number = number[:64] if number else None
        entity.invoice_date = _as_date(_usable(f, "invoice_date"))
        entity.grand_total = _as_decimal(_usable(f, "grand_total"))
    elif isinstance(entity, BankStatement):
        account = _usable(f, "account_number")
        entity.account_number = account[:32] if account else None
        entity.period_from = _as_date(_usable(f, "period_from"))
        entity.period_to = _as_date(_usable(f, "period_to"))
    elif isinstance(entity, BankTransaction):
        entity.txn_date = _as_date(_usable(f, "txn_date"))
        entity.debit = _as_decimal(_usable(f, "debit"))
        entity.credit = _as_decimal(_usable(f, "credit"))
        entity.balance = _as_decimal(_usable(f, "balance"))
        reference = _usable(f, "reference_number")
        entity.reference_number = reference[:64] if reference else None


# ---- Saving -------------------------------------------------------------------------

def save_extraction(session: Session, job_id: int, document: Document, result: ExtractionResult) -> ExtractionRun:
    run = ExtractionRun(
        job_id=job_id, document_id=document.id, document_type=result.document_type,
        provider=result.provider, model=result.model, prompt_version=result.prompt_version,
        llm_calls=result.calls, prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens, seconds=result.seconds,
        document_issues=result.document_check_issues, raw_responses=result.raw_responses,
    )
    session.add(run)
    header = {name: f.to_meta() for name, f in result.header.items()}
    if result.document_type is DocumentType.PURCHASE_INVOICE:
        invoice = Invoice(run=run, document_id=document.id, fields=header)
        sync_key_columns(invoice)
        session.add(invoice)
        for number, row in enumerate(result.rows, start=1):
            session.add(InvoiceLineItem(
                invoice=invoice, line_number=number, row_text=row.row_text, page=row.page,
                confidence=row.confidence, fields={n: f.to_meta() for n, f in row.fields.items()},
            ))
    else:
        statement = BankStatement(run=run, document_id=document.id, fields=header)
        sync_key_columns(statement)
        session.add(statement)
        for number, row in enumerate(result.rows, start=1):
            transaction = BankTransaction(
                statement=statement, sequence=number, row_text=row.row_text, page=row.page,
                confidence=row.confidence, fields={n: f.to_meta() for n, f in row.fields.items()},
            )
            sync_key_columns(transaction)
            session.add(transaction)
    return run


# ---- Editing --------------------------------------------------------------------------

def _required(entity: Entity, name: str) -> bool:
    if isinstance(entity, Invoice):
        return name in REQUIRED_FIELDS[DocumentType.PURCHASE_INVOICE]
    if isinstance(entity, BankStatement):
        return name in REQUIRED_FIELDS[DocumentType.BANK_STATEMENT]
    return False


def apply_edit(
    session: Session, kind: EditableEntity, entity_id: int, field_name: str, new_raw: str | None,
    *, reason: str | None, client_address: str | None, user_id: int | None = None,
) -> tuple[Entity, dict[str, Any]]:
    """Replace one field's value with reviewer input, re-check its format, audit it.

    The reviewer's text is parsed by the same rules as extracted text. The
    original extracted value and its evidence are preserved under ``original``.
    """
    specs = {s.name: s for s in ENTITY_FIELDS[kind]}
    spec = specs.get(field_name)
    if spec is None:
        raise EditError("unknown_field", f"'{field_name}' is not a field of {kind.value}.")
    entity = session.get(ENTITY_MODELS[kind], entity_id)
    if entity is None:
        raise EditError("not_found", f"{kind.value} {entity_id} not found.")

    new_raw = new_raw.strip() if new_raw and new_raw.strip() else None
    fields = copy.deepcopy(entity.fields)
    current = fields.get(field_name) or {"raw": None, "value": None}
    if new_raw == current.get("raw"):
        raise EditError("unchanged", "The new value is the same as the current one.")

    parsed = parse_value(new_raw, spec.value_type)
    updated = {
        "type": spec.value_type.value, "raw": new_raw, "value": parsed.value, "status": parsed.status.value,
        "method": "edited",
        "flags": ["format_invalid"] if parsed.status is Status.INVALID else [],
        "messages": list(parsed.messages), "evidence": None, "page": None, "confidence": None,
        "alternatives": [], "edited_at": utc_now().isoformat(),
        # The first extracted version is kept for comparison and audit.
        "original": current.get("original") or {
            k: current.get(k) for k in ("raw", "value", "status", "evidence", "page", "confidence")
        },
    }
    if new_raw is None and _required(entity, field_name):
        updated["flags"].append("required_missing")
        updated["messages"].append("Required field not found in the document.")
    fields[field_name] = updated
    entity.fields = fields  # reassign: JSON columns do not track in-place changes
    sync_key_columns(entity)
    session.add(FieldEdit(
        entity=kind, entity_id=entity_id, field_name=field_name, old_raw=current.get("raw"),
        new_raw=new_raw, reason=reason, edited_by_user_id=user_id, client_address=client_address,
    ))
    return entity, updated
