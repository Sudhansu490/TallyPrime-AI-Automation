"""Import every model here so Alembic autogenerate sees the full metadata."""

from tally_automation.db.models.user import AppUser, UserRole

__all__ = ["AppUser", "UserRole"]
