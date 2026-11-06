"""Generated TallyPrime vouchers and remembered ledger choices (Phase 7).

* ``GeneratedVoucher`` – one voucher built from one source record (an invoice
  or a bank transaction) with the ledgers a reviewer chose, its TallyPrime
  import XML, and the TallyPrime-compatibility check results. Generating
  again for the same source supersedes the previous voucher; at most one
  voucher per source is current (partial unique index). Generation never
  contacts TallyPrime: importing is a separate, approved operation (Phase 8).
* ``MappingMemory`` – the ledger a reviewer last chose for a recurring key
  (a supplier GSTIN, a bank account, a GST head, …). It only *suggests*;
  every voucher still uses the ledgers submitted with its generation request.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, CheckConstraint, Date, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from tally_automation.db.base import Base, DecimalText, UtcDateTime, str_enum, utc_now


class VoucherKind(StrEnum):
    PURCHASE = "purchase"
    PAYMENT = "payment"
    RECEIPT = "receipt"
    CONTRA = "contra"


class VoucherSource(StrEnum):
    INVOICE = "invoice"
    BANK_TRANSACTION = "bank_transaction"


class VoucherStatus(StrEnum):
    READY = "ready"              # all TallyPrime checks pass; may go to approval (Phase 8)
    BLOCKED = "blocked"          # at least one error; cannot be approved
    SUPERSEDED = "superseded"    # replaced by a later generation for the same source


class MappingPurpose(StrEnum):
    PARTY = "party"                    # key: supplier GSTIN (or normalised name)
    PURCHASE = "purchase"              # key: GST rate bucket, e.g. "18" or "all"
    DUTY = "duty"                      # key: CGST / SGST / IGST / CESS
    ROUND_OFF = "round_off"            # key: "round_off"
    VOUCHER_TYPE = "voucher_type"      # key: voucher kind; stores the type name (no GUID needed)
    BANK = "bank"                      # key: statement account number
    COUNTER = "counter"                # key: normalised transaction description


class GeneratedVoucher(Base):
    __tablename__ = "generated_vouchers"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="RESTRICT"), index=True)
    extraction_run_id: Mapped[int] = mapped_column(ForeignKey("extraction_runs.id", ondelete="RESTRICT"))
    source_kind: Mapped[VoucherSource] = mapped_column(str_enum(VoucherSource, "voucher_source"))
    source_id: Mapped[int] = mapped_column(Integer)
    # SHA-256 of the source record's reviewed fields at generation: a later edit
    # makes the voucher stale (it must be generated again before approval).
    source_digest: Mapped[str] = mapped_column(String(64))
    company_guid: Mapped[str] = mapped_column(String(64))
    company_name: Mapped[str] = mapped_column(String(255))
    # The masters snapshot the ledgers were checked against.
    masters_sync_run_id: Mapped[int] = mapped_column(ForeignKey("tally_sync_runs.id", ondelete="RESTRICT"))
    voucher_kind: Mapped[VoucherKind] = mapped_column(str_enum(VoucherKind, "voucher_kind"))
    voucher_type_name: Mapped[str | None] = mapped_column(String(255))
    voucher_date: Mapped[date | None] = mapped_column(Date)
    reference: Mapped[str | None] = mapped_column(String(255))
    party_ledger_name: Mapped[str | None] = mapped_column(String(255))
    amount: Mapped[Decimal | None] = mapped_column(DecimalText())
    # Marker written into the narration so a later import (Phase 8) can find
    # this voucher in TallyPrime and refuse to post it twice.
    marker: Mapped[str] = mapped_column(String(64))
    entries: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    mapping: Mapped[dict[str, Any]] = mapped_column(JSON)
    narration: Mapped[str | None] = mapped_column(Text)
    xml: Mapped[str | None] = mapped_column(Text)
    xml_sha256: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[VoucherStatus] = mapped_column(str_enum(VoucherStatus, "voucher_status"))
    error_count: Mapped[int] = mapped_column(Integer)
    warning_count: Mapped[int] = mapped_column(Integer)
    checks: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    rules_version: Mapped[str] = mapped_column(String(32))
    # Login arrives in Phase 9; until then the generating device is recorded.
    generated_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("app_users.id", ondelete="RESTRICT"))
    client_address: Mapped[str | None] = mapped_column(String(64))
    generated_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utc_now)
    superseded_at: Mapped[datetime | None] = mapped_column(UtcDateTime())

    __table_args__ = (
        CheckConstraint(
            "status != 'ready' OR (xml IS NOT NULL AND xml_sha256 IS NOT NULL AND error_count = 0)",
            name="ready_has_valid_xml",
        ),
        CheckConstraint("(status = 'superseded') = (superseded_at IS NOT NULL)", name="superseded_has_time"),
        CheckConstraint("amount IS NULL OR CAST(amount AS REAL) > 0", name="amount_positive"),
        # At most one current (not superseded) voucher per source record.
        Index(
            "uq_generated_vouchers_current_source", "source_kind", "source_id", unique=True,
            sqlite_where=text("status != 'superseded'"),
        ),
        Index("ix_generated_vouchers_marker", "marker"),
    )


class MappingMemory(Base):
    __tablename__ = "mapping_memory"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_guid: Mapped[str] = mapped_column(String(64))
    purpose: Mapped[MappingPurpose] = mapped_column(str_enum(MappingPurpose, "mapping_purpose"))
    key: Mapped[str] = mapped_column(String(128))
    ledger_guid: Mapped[str | None] = mapped_column(String(64))   # NULL for voucher types
    name: Mapped[str] = mapped_column(String(255))
    use_count: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utc_now, onupdate=utc_now)

    __table_args__ = (UniqueConstraint("company_guid", "purpose", "key"),)
