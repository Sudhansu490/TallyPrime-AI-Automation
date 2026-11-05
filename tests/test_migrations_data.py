"""Migrations must preserve existing rows. SYNTHETIC rows only."""

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from tally_automation.config import Settings

PHASE2_REVISION = "07724a6d6a7a"
SHA_A = "a" * 64


def _seed_phase2(url: str) -> None:
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO document_files (id, sha256, size_bytes, media_type, storage_path, page_count, created_at)"
            " VALUES (1, :sha, 10, 'application/pdf', 'originals/aa/x.pdf', 1, CURRENT_TIMESTAMP)"
        ), {"sha": SHA_A})
        connection.execute(text(
            "INSERT INTO documents (id, file_id, document_type, original_filename, status, duplicate_of_id)"
            " VALUES (1, 1, 'purchase_invoice', 'synthetic.pdf', 'queued', NULL),"
            "        (2, 1, 'purchase_invoice', 'synthetic copy.pdf', 'duplicate', 1)"
        ))
        connection.execute(text(
            "INSERT INTO processing_jobs (id, document_id, attempt, status, text_extraction_status, ocr_status,"
            " ai_extraction_status, validation_status) VALUES (1, 1, 1, 'queued', 'pending', 'pending', 'pending', 'pending')"
        ))
    engine.dispose()


def test_upgrade_and_downgrade_keep_existing_documents(alembic_config: Config, settings: Settings) -> None:
    command.upgrade(alembic_config, PHASE2_REVISION)
    _seed_phase2(settings.database_url)

    command.upgrade(alembic_config, "head")
    engine = create_engine(settings.database_url)
    with engine.begin() as connection:
        rows = connection.execute(text("SELECT id, status, duplicate_of_id FROM documents ORDER BY id")).all()
        assert rows == [(1, "queued", None), (2, "duplicate", 1)]
        assert connection.execute(text("SELECT count(*) FROM processing_jobs")).scalar() == 1
        connection.execute(text("UPDATE documents SET status = 'text_extracted' WHERE id = 1"))
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    engine.dispose()

    command.downgrade(alembic_config, PHASE2_REVISION)
    engine = create_engine(settings.database_url)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT status FROM documents WHERE id = 1")).scalar() == "queued"
        assert connection.execute(text("SELECT count(*) FROM documents")).scalar() == 2
    engine.dispose()
