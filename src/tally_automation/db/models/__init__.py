"""Import every model here so Alembic autogenerate sees the full metadata."""

from tally_automation.db.models.document import (
    Document,
    DocumentFile,
    DocumentPage,
    DocumentStatus,
    DocumentType,
    JobStatus,
    MediaType,
    ProcessingJob,
    ProcessingStage,
    StageStatus,
    TextSource,
)
from tally_automation.db.models.extraction import (
    BankStatement,
    BankTransaction,
    EditableEntity,
    ExtractionRun,
    FieldEdit,
    Invoice,
    InvoiceLineItem,
)
from tally_automation.db.models.user import AppUser, UserRole

__all__ = [
    "AppUser",
    "BankStatement",
    "BankTransaction",
    "EditableEntity",
    "ExtractionRun",
    "FieldEdit",
    "Invoice",
    "InvoiceLineItem",
    "Document",
    "DocumentFile",
    "DocumentPage",
    "DocumentStatus",
    "DocumentType",
    "JobStatus",
    "MediaType",
    "ProcessingJob",
    "ProcessingStage",
    "StageStatus",
    "TextSource",
    "UserRole",
]
