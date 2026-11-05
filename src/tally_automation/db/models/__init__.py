"""Import every model here so Alembic autogenerate sees the full metadata."""

from tally_automation.db.models.document import (
    Document,
    DocumentFile,
    DocumentStatus,
    DocumentType,
    JobStatus,
    MediaType,
    ProcessingJob,
    ProcessingStage,
    StageStatus,
)
from tally_automation.db.models.user import AppUser, UserRole

__all__ = [
    "AppUser",
    "Document",
    "DocumentFile",
    "DocumentStatus",
    "DocumentType",
    "JobStatus",
    "MediaType",
    "ProcessingJob",
    "ProcessingStage",
    "StageStatus",
    "UserRole",
]
