"""Bank reconciliation (read-only towards statements and TallyPrime).

* ``ReconciliationAccount`` – one bank account seen on statements (identified by
  its account number as printed), and the TallyPrime bank ledger a reviewer
  linked it to. Balances are never stored here: they are worked out from the
  statements' reviewed fields every time, so an edit on the Review page is
  reflected at once and extracted values are never copied or changed.
* ``ReconciliationRun`` – one saved reconciliation (monthly, yearly, or a
  transaction comparison with TallyPrime), exactly as it was worked out:
  reports are made from it, so a report can always be reproduced. Never
  updated.
* ``ReconciliationDecision`` – the current approval or rejection of one
  proposed match (by its signature), per account. Applied whenever a
  comparison is shown; the history of decisions is in the events.
* ``ReconciliationEvent`` – append-only audit trail: ledger linked / unlinked,
  runs saved, reports downloaded, matches approved / rejected / reopened.
  Never updated or deleted.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, CheckConstraint, Date, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from tally_automation.db.base import Base, TimestampMixin, UtcDateTime, str_enum, utc_now


class ReconciliationEventKind(StrEnum):
    LEDGER_LINKED = "ledger_linked"
    LEDGER_UNLINKED = "ledger_unlinked"
    RUN_SAVED = "run_saved"
    REPORT_DOWNLOADED = "report_downloaded"
    MATCH_APPROVED = "match_approved"
    MATCH_REJECTED = "match_rejected"
    MATCH_REOPENED = "match_reopened"


class RunKind(StrEnum):
    MONTHLY = "monthly"
    YEARLY = "yearly"
    TRANSACTIONS = "transactions"


class MatchDecision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


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
    run_id: Mapped[int | None] = mapped_column(Integer)      # the run concerned, if any (no FK: added later)

    __table_args__ = (Index("ix_reconciliation_events_account_id", "account_id"),)


class ReconciliationRun(Base):
    """One saved reconciliation, as it was worked out. Never updated."""

    __tablename__ = "reconciliation_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_accounts.id", ondelete="RESTRICT"))
    kind: Mapped[RunKind] = mapped_column(str_enum(RunKind, "reconciliation_run_kind"))
    period_from: Mapped[date] = mapped_column(Date)
    period_to: Mapped[date] = mapped_column(Date)
    label: Mapped[str] = mapped_column(String(64))
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON)
    # Amounts as decimal strings; for a transaction run, the matches before any decision.
    result: Mapped[dict[str, Any]] = mapped_column(JSON)
    # SHA-256 of the statement figures used: a later correction makes the run out of date.
    statements_digest: Mapped[str] = mapped_column(String(64))
    company_guid: Mapped[str | None] = mapped_column(String(64))
    ledger_guid: Mapped[str | None] = mapped_column(String(64))
    ledger_name: Mapped[str | None] = mapped_column(String(255))
    rules_version: Mapped[str] = mapped_column(String(32))
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("app_users.id", ondelete="RESTRICT"))
    client_address: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utc_now)

    __table_args__ = (
        CheckConstraint("period_from <= period_to", name="period_ordered"),
        CheckConstraint("kind != 'transactions' OR ledger_guid IS NOT NULL", name="transactions_have_ledger"),
        Index("ix_reconciliation_runs_account_id", "account_id"),
    )


class ReconciliationDecision(Base):
    """The current decision on one proposed match. Reopening removes it; every change is an event."""

    __tablename__ = "reconciliation_decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_accounts.id", ondelete="RESTRICT"))
    signature: Mapped[str] = mapped_column(String(64))
    decision: Mapped[MatchDecision] = mapped_column(str_enum(MatchDecision, "reconciliation_match_decision"))
    note: Mapped[str | None] = mapped_column(Text)
    run_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_runs.id", ondelete="RESTRICT"))
    decided_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("app_users.id", ondelete="RESTRICT"))
    client_address: Mapped[str | None] = mapped_column(String(64))
    decided_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utc_now)

    __table_args__ = (UniqueConstraint("account_id", "signature"),)
