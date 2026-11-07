"""TallyPrime master data retrieved read-only (Phase 6).

* ``TallySyncRun`` – one retrieval attempt (who/where/when, counts, error).
* ``TallyCompany`` – a company seen loaded in TallyPrime.
* ``TallyGroup`` / ``TallyLedger`` / ``TallyVoucherType`` – the company's
  masters as of its last successful retrieval. A retrieval replaces them in
  one transaction, so they are always one consistent snapshot. Records are
  identified by Tally's GUID; later phases refer to them by GUID and name,
  never by these tables' row ids.

Nothing here is ever written back to TallyPrime.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    JSON, Boolean, CheckConstraint, Date, ForeignKey, Index, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from tally_automation.db.base import Base, DecimalText, UtcDateTime, str_enum, utc_now


class SyncStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class TallySyncRun(Base):
    __tablename__ = "tally_sync_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_name: Mapped[str] = mapped_column(String(255))
    company_guid: Mapped[str | None] = mapped_column(String(64))
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer)
    status: Mapped[SyncStatus] = mapped_column(str_enum(SyncStatus, "sync_status"))
    group_count: Mapped[int | None] = mapped_column(Integer)
    ledger_count: Mapped[int | None] = mapped_column(Integer)
    voucher_type_count: Mapped[int | None] = mapped_column(Integer)
    # XML-illegal control characters TallyPrime sent and the parser removed.
    removed_characters: Mapped[int | None] = mapped_column(Integer)
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("app_users.id", ondelete="RESTRICT"))
    started_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime())

    __table_args__ = (
        CheckConstraint("(status = 'failed') = (error_message IS NOT NULL)", name="failed_has_error"),
        Index("ix_tally_sync_runs_company_guid", "company_guid"),
    )


class TallyCompany(Base):
    __tablename__ = "tally_companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    guid: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    books_from: Mapped[date | None] = mapped_column(Date)
    starting_from: Mapped[date | None] = mapped_column(Date)
    state: Mapped[str | None] = mapped_column(String(128))
    country: Mapped[str | None] = mapped_column(String(128))
    last_sync_run_id: Mapped[int | None] = mapped_column(ForeignKey("tally_sync_runs.id", ondelete="RESTRICT"))
    last_synced_at: Mapped[datetime | None] = mapped_column(UtcDateTime())


class TallyGroup(Base):
    __tablename__ = "tally_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_guid: Mapped[str] = mapped_column(String(64))
    sync_run_id: Mapped[int] = mapped_column(ForeignKey("tally_sync_runs.id", ondelete="RESTRICT"))
    guid: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(255))
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    parent: Mapped[str | None] = mapped_column(String(255))
    primary_group: Mapped[str | None] = mapped_column(String(255))
    is_revenue: Mapped[bool | None] = mapped_column(Boolean)
    is_deemed_positive: Mapped[bool | None] = mapped_column(Boolean)
    affects_gross_profit: Mapped[bool | None] = mapped_column(Boolean)
    alter_id: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        UniqueConstraint("company_guid", "guid"),
        Index("ix_tally_groups_company_name", "company_guid", "name"),
    )


class TallyLedger(Base):
    __tablename__ = "tally_ledgers"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_guid: Mapped[str] = mapped_column(String(64))
    sync_run_id: Mapped[int] = mapped_column(ForeignKey("tally_sync_runs.id", ondelete="RESTRICT"))
    guid: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(255))
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    parent: Mapped[str | None] = mapped_column(String(255))
    primary_group: Mapped[str | None] = mapped_column(String(255))
    gstin: Mapped[str | None] = mapped_column(String(32))
    # GSTIN passes the format and check-character test (parsing.parse_gstin).
    gstin_valid: Mapped[bool | None] = mapped_column(Boolean)
    gst_registration_type: Mapped[str | None] = mapped_column(String(64))
    state: Mapped[str | None] = mapped_column(String(128))
    country: Mapped[str | None] = mapped_column(String(128))
    tax_type: Mapped[str | None] = mapped_column(String(64))
    gst_duty_head: Mapped[str | None] = mapped_column(String(32))
    tax_rate: Mapped[Decimal | None] = mapped_column(DecimalText())
    # Bank ledgers only. Stored in full for matching statements; API responses mask it.
    bank_account_number: Mapped[str | None] = mapped_column(String(64))
    ifsc: Mapped[str | None] = mapped_column(String(32))
    is_bill_wise: Mapped[bool | None] = mapped_column(Boolean)
    alter_id: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        UniqueConstraint("company_guid", "guid"),
        Index("ix_tally_ledgers_company_name", "company_guid", "name"),
        Index("ix_tally_ledgers_company_gstin", "company_guid", "gstin"),
    )


class TallyVoucherType(Base):
    __tablename__ = "tally_voucher_types"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_guid: Mapped[str] = mapped_column(String(64))
    sync_run_id: Mapped[int] = mapped_column(ForeignKey("tally_sync_runs.id", ondelete="RESTRICT"))
    guid: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(255))
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    parent: Mapped[str | None] = mapped_column(String(255))
    numbering_method: Mapped[str | None] = mapped_column(String(64))
    is_active: Mapped[bool | None] = mapped_column(Boolean)
    alter_id: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (UniqueConstraint("company_guid", "guid"),)
