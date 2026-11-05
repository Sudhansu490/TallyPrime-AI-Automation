"""Accounting validation results and duplicate matches (Phase 5).

* ``ValidationResult`` – the latest accounting/extraction check results for
  one extraction run. Recomputed after every reviewer edit; the edits
  themselves are audited in ``field_edits``.
* ``DuplicateMatch`` – a possible business-level duplicate between two
  documents (Level 2: invoice, Level 3: bank transaction), with the matching
  evidence. Nothing is rejected automatically: a person decides.
* ``DuplicateReview`` – append-only audit trail of those decisions.

Level 1 (byte-identical files) is recorded on ``documents`` at upload.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from tally_automation.db.base import Base, TimestampMixin, UtcDateTime, str_enum, utc_now


class ValidationOutcome(StrEnum):
    PASSED = "passed"
    WARNINGS = "passed_with_warnings"   # usable, but a person should read the warnings
    FAILED = "failed"                   # at least one error: blocks voucher generation


class DuplicateKind(StrEnum):
    INVOICE = "invoice"                     # Level 2
    BANK_TRANSACTION = "bank_transaction"   # Level 3


class MatchStrength(StrEnum):
    PROBABLE = "probable"   # strong identifiers agree
    POSSIBLE = "possible"   # weaker evidence; may well be a different record


class MatchStatus(StrEnum):
    OPEN = "open"                       # not reviewed yet
    DUPLICATE = "duplicate"             # confirmed: the newer record is a duplicate
    NOT_DUPLICATE = "not_duplicate"     # confirmed: both records are genuine


class ReviewDecision(StrEnum):
    DUPLICATE = "duplicate"
    NOT_DUPLICATE = "not_duplicate"
    REOPENED = "reopened"


class ValidationResult(Base):
    __tablename__ = "validation_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    extraction_run_id: Mapped[int] = mapped_column(
        ForeignKey("extraction_runs.id", ondelete="RESTRICT"), unique=True
    )
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="RESTRICT"), index=True)
    rules_version: Mapped[str] = mapped_column(String(32))
    outcome: Mapped[ValidationOutcome] = mapped_column(str_enum(ValidationOutcome, "validation_outcome"))
    error_count: Mapped[int] = mapped_column(Integer)
    warning_count: Mapped[int] = mapped_column(Integer)
    # [{code, category, severity, passed, message, row, fields, calculated, printed, difference}]
    checks: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    validated_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utc_now)

    __table_args__ = (
        CheckConstraint("error_count >= 0 AND warning_count >= 0", name="counts_not_negative"),
        CheckConstraint(
            "(outcome = 'failed') = (error_count > 0)", name="failed_iff_errors"
        ),
    )


class DuplicateMatch(TimestampMixin, Base):
    __tablename__ = "duplicate_matches"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[DuplicateKind] = mapped_column(str_enum(DuplicateKind, "duplicate_kind"))
    # "<kind>:<smaller entity id>:<larger entity id>": one row per pair, enforced
    # by the database so concurrent detection cannot record a pair twice.
    pair_key: Mapped[str] = mapped_column(String(80), unique=True)
    # ``document_id`` is the later upload (the candidate duplicate);
    # ``other_document_id`` the earlier one it resembles.
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="RESTRICT"), index=True)
    other_document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="RESTRICT"), index=True
    )
    # Invoice ids or bank transaction ids, depending on ``kind``.
    entity_id: Mapped[int] = mapped_column(Integer)
    other_entity_id: Mapped[int] = mapped_column(Integer)
    strength: Mapped[MatchStrength] = mapped_column(str_enum(MatchStrength, "match_strength"))
    rule: Mapped[str] = mapped_column(String(64))
    explanation: Mapped[str] = mapped_column(Text)
    # [{"field", "label", "this", "other", "match": "same" | "similar" | "different" | "unknown"}]
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    status: Mapped[MatchStatus] = mapped_column(
        str_enum(MatchStatus, "match_status"), default=MatchStatus.OPEN
    )
    resolution_note: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(UtcDateTime())

    reviews: Mapped[list[DuplicateReview]] = relationship(
        back_populates="match", order_by="DuplicateReview.id"
    )

    __table_args__ = (
        CheckConstraint("document_id <> other_document_id", name="different_documents"),
        CheckConstraint("entity_id <> other_entity_id", name="different_entities"),
        CheckConstraint("(status = 'open') = (resolved_at IS NULL)", name="resolved_has_time"),
        Index("ix_duplicate_matches_status", "status"),
    )


class DuplicateReview(Base):
    """Append-only record of one duplicate decision. Never updated or deleted."""

    __tablename__ = "duplicate_reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("duplicate_matches.id", ondelete="RESTRICT"), index=True)
    decision: Mapped[ReviewDecision] = mapped_column(str_enum(ReviewDecision, "review_decision"))
    note: Mapped[str | None] = mapped_column(Text)
    # Login arrives in Phase 9; until then the device address identifies the reviewer.
    reviewed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("app_users.id", ondelete="RESTRICT"))
    client_address: Mapped[str | None] = mapped_column(String(64))
    reviewed_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utc_now)

    match: Mapped[DuplicateMatch] = relationship(back_populates="reviews")
