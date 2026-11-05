"""AI extraction results.

* ``ExtractionRun`` – one LLM extraction for one processing job (provider,
  model, prompt version, token use, and the raw validated replies).
* ``Invoice`` / ``InvoiceLineItem`` and ``BankStatement`` / ``BankTransaction``
  – the extracted entities.
* ``FieldEdit`` – append-only audit trail of every reviewer edit.

Each entity keeps its fields in ``fields`` (JSON), one entry per field::

    {"raw": "1,180.00", "value": "1180.00", "type": "amount", "status": "valid",
     "method": "extracted" | "calculated" | "edited", "flags": [...], "messages": [...],
     "evidence": "...", "page": 1, "confidence": 0.98, "alternatives": [...]}

so value, source evidence, confidence, extraction method and validation
status travel together. A few typed, indexed *key columns* (GSTIN, invoice
number, dates, amounts) mirror the reviewed values for querying and for
duplicate detection in Phase 5; they are set only from valid parsed values.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON, CheckConstraint, Date, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from tally_automation.db.base import Base, DecimalText, TimestampMixin, UtcDateTime, str_enum, utc_now
from tally_automation.db.models.document import DocumentType

FieldsJson = dict[str, dict[str, Any]]


class EditableEntity(StrEnum):
    INVOICE = "invoice"
    INVOICE_LINE_ITEM = "invoice_line_item"
    BANK_STATEMENT = "bank_statement"
    BANK_TRANSACTION = "bank_transaction"


class ExtractionRun(Base):
    __tablename__ = "extraction_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("processing_jobs.id", ondelete="RESTRICT"), unique=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="RESTRICT"), index=True)
    document_type: Mapped[DocumentType] = mapped_column(str_enum(DocumentType, "extraction_document_type"))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(32))
    llm_calls: Mapped[int] = mapped_column(Integer)
    prompt_tokens: Mapped[int] = mapped_column(Integer)
    completion_tokens: Mapped[int] = mapped_column(Integer)
    seconds: Mapped[float] = mapped_column(Float)
    # Whole-document problems, e.g. "not an invoice" or "no transactions found".
    document_issues: Mapped[list[str]] = mapped_column(JSON)
    # The schema-validated JSON replies, kept for audit and prompt debugging.
    raw_responses: Mapped[list[str]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utc_now)

    invoice: Mapped[Invoice | None] = relationship(back_populates="run")
    bank_statement: Mapped[BankStatement | None] = relationship(back_populates="run")


class Invoice(TimestampMixin, Base):
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(primary_key=True)
    extraction_run_id: Mapped[int] = mapped_column(
        ForeignKey("extraction_runs.id", ondelete="RESTRICT"), unique=True
    )
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="RESTRICT"), index=True)
    fields: Mapped[FieldsJson] = mapped_column(JSON)
    supplier_gstin: Mapped[str | None] = mapped_column(String(15))
    invoice_number: Mapped[str | None] = mapped_column(String(64))
    invoice_date: Mapped[date | None] = mapped_column(Date)
    grand_total: Mapped[Decimal | None] = mapped_column(DecimalText())

    run: Mapped[ExtractionRun] = relationship(back_populates="invoice")
    line_items: Mapped[list[InvoiceLineItem]] = relationship(
        back_populates="invoice", order_by="InvoiceLineItem.line_number"
    )

    __table_args__ = (Index("ix_invoices_supplier_gstin_invoice_number", "supplier_gstin", "invoice_number"),)


class InvoiceLineItem(TimestampMixin, Base):
    __tablename__ = "invoice_line_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id", ondelete="RESTRICT"))
    line_number: Mapped[int] = mapped_column(Integer)
    row_text: Mapped[str | None] = mapped_column(Text)
    page: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[float] = mapped_column(Float)
    fields: Mapped[FieldsJson] = mapped_column(JSON)

    invoice: Mapped[Invoice] = relationship(back_populates="line_items")

    __table_args__ = (
        UniqueConstraint("invoice_id", "line_number"),
        CheckConstraint("line_number > 0", name="line_number_positive"),
    )


class BankStatement(TimestampMixin, Base):
    __tablename__ = "bank_statements"

    id: Mapped[int] = mapped_column(primary_key=True)
    extraction_run_id: Mapped[int] = mapped_column(
        ForeignKey("extraction_runs.id", ondelete="RESTRICT"), unique=True
    )
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="RESTRICT"), index=True)
    fields: Mapped[FieldsJson] = mapped_column(JSON)
    # Stored in full (needed for matching); API responses mask it.
    account_number: Mapped[str | None] = mapped_column(String(32), index=True)
    period_from: Mapped[date | None] = mapped_column(Date)
    period_to: Mapped[date | None] = mapped_column(Date)

    run: Mapped[ExtractionRun] = relationship(back_populates="bank_statement")
    transactions: Mapped[list[BankTransaction]] = relationship(
        back_populates="statement", order_by="BankTransaction.sequence"
    )


class BankTransaction(TimestampMixin, Base):
    __tablename__ = "bank_transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    bank_statement_id: Mapped[int] = mapped_column(ForeignKey("bank_statements.id", ondelete="RESTRICT"))
    sequence: Mapped[int] = mapped_column(Integer)
    row_text: Mapped[str | None] = mapped_column(Text)
    page: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[float] = mapped_column(Float)
    fields: Mapped[FieldsJson] = mapped_column(JSON)
    txn_date: Mapped[date | None] = mapped_column(Date, index=True)
    debit: Mapped[Decimal | None] = mapped_column(DecimalText())
    credit: Mapped[Decimal | None] = mapped_column(DecimalText())
    balance: Mapped[Decimal | None] = mapped_column(DecimalText())
    reference_number: Mapped[str | None] = mapped_column(String(64))

    statement: Mapped[BankStatement] = relationship(back_populates="transactions")

    __table_args__ = (
        UniqueConstraint("bank_statement_id", "sequence"),
        CheckConstraint("sequence > 0", name="sequence_positive"),
    )


class FieldEdit(Base):
    """Append-only record of one reviewer edit. Never updated or deleted."""

    __tablename__ = "field_edits"

    id: Mapped[int] = mapped_column(primary_key=True)
    entity: Mapped[EditableEntity] = mapped_column(str_enum(EditableEntity, "editable_entity"))
    entity_id: Mapped[int] = mapped_column(Integer)
    field_name: Mapped[str] = mapped_column(String(64))
    old_raw: Mapped[str | None] = mapped_column(Text)
    new_raw: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    # Login arrives in Phase 9. Until then the user is unknown (NULL) and the
    # editing device is identified by its (Tailscale) address.
    edited_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("app_users.id", ondelete="RESTRICT"))
    client_address: Mapped[str | None] = mapped_column(String(64))
    edited_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utc_now)

    __table_args__ = (Index("ix_field_edits_entity_entity_id", "entity", "entity_id"),)
