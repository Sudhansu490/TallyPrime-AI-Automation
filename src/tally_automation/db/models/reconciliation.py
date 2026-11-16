"""Bank reconciliation (read-only towards statements and TallyPrime).

* ``ReconciliationAccount`` – one bank account seen on statements (identified by
  its account number as printed), and the TallyPrime bank ledger a reviewer
  linked it to. Balances are never stored here: they are worked out from the
  statements' reviewed fields every time, so an edit on the Review page is
  reflected at once and extracted values are never copied or changed.
* ``ReconciliationEvent`` – append-only audit trail: ledger linked / unlinked,
  and (later) reconciliation runs, report downloads and match decisions.
  Never updated or deleted.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from tally_automation.db.base import Base, TimestampMixin, UtcDateTime, str_enum, utc_now


class ReconciliationEventKind(StrEnum):
    LEDGER_LINKED = "ledger_linked"
    LEDGER_UNLINKED = "ledger_unlinked"


class ReconciliationAccount(TimestampMixin, Base):
    __tablename__ = "reconciliation_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    # SHA-256 of the normalised account number: the account's id in URLs, so the
    # number itself never appears in addresses or logs.
    account_key: Mapped[str] = mapped_column(String(64), unique=True)
    # Stored in full (it identifies the statements); API responses mask it.
    account_number: Mapped[str] = mapped_column(String(32))
    company_guid: Mapped[str | None] = mapped_column(String(64))
    ledger_guid: Mapped[str | None] = mapped_column(String(64))
    ledger_name: Mapped[str | None] = mapped_column(String(255))
    linked_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("app_users.id", ondelete="RESTRICT"))
    linked_at: Mapped[datetime | None] = mapped_column(UtcDateTime())

    __table_args__ = (
        CheckConstraint(
            "(company_guid IS NULL) = (ledger_guid IS NULL) AND (ledger_guid IS NULL) = (ledger_name IS NULL)"
            " AND (ledger_guid IS NULL) = (linked_at IS NULL)",
            name="link_complete",
        ),
    )


class ReconciliationEvent(Base):
    """Append-only record of one reconciliation action. Never updated or deleted."""

    __tablename__ = "reconciliation_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_accounts.id", ondelete="RESTRICT"))
    kind: Mapped[ReconciliationEventKind] = mapped_column(str_enum(ReconciliationEventKind, "reconciliation_event_kind"))
    # What changed, e.g. {"company_guid", "ledger_guid", "ledger_name", "previous": {...}}.
    detail: Mapped[dict[str, Any]] = mapped_column(JSON)
    note: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("app_users.id", ondelete="RESTRICT"))
    client_address: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utc_now)

    __table_args__ = (Index("ix_reconciliation_events_account_id", "account_id"),)
