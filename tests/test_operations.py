"""Migration, backup and pre-flight check commands (Phase 10). SYNTHETIC data only."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tally_automation import operations as ops
from tally_automation.cli import main as cli
from tally_automation.config import Settings
from tally_automation.db.models import UserRole
from tally_automation.server import run
from test_extraction_api import text_pdf, upload
from extraction_fakes import INVOICE_TEXT


def test_schema_status_and_migrate(settings: Settings) -> None:
    current, head = ops.schema_status(settings)
    assert current is None and head
    before, after = ops.migrate(settings)
    assert (before, after) == (None, head)
    assert ops.schema_status(settings) == (head, head)


def test_server_refuses_an_outdated_schema(settings: Settings) -> None:
    with pytest.raises(SystemExit):
        run(settings)            # empty database: refused before any socket is opened


def test_backup_is_consistent_incremental_and_rotated(client: TestClient, settings: Settings, tmp_path: Path) -> None:
    upload(client, text_pdf(INVOICE_TEXT))
    target = settings.model_copy(update={"backup_dir_setting": tmp_path / "elsewhere", "backup_keep": 2})
    first = ops.backup(target, now=datetime(2026, 11, 7, 1, tzinfo=UTC))
    assert first.database_copy.name == "tally_automation-20261107-010000.db"
    assert (first.documents_copied, first.documents_total) == (1, 1)
    copy = sqlite3.connect(first.database_copy)
    try:
        assert copy.execute("select count(*) from documents").fetchone()[0] == 1
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        copy.close()
    second = ops.backup(target, now=datetime(2026, 11, 7, 2, tzinfo=UTC))
    assert second.documents_copied == 0                       # unchanged documents are not copied again
    third = ops.backup(target, now=datetime(2026, 11, 7, 3, tzinfo=UTC))
    assert third.removed_old == 1
    assert sorted(p.name for p in (tmp_path / "elsewhere").glob("*.db")) == [
        "tally_automation-20261107-020000.db", "tally_automation-20261107-030000.db"]
    assert not list((tmp_path / "elsewhere").glob("*.partial"))


def test_preflight_reports_problems_without_secrets(client: TestClient, settings: Settings, monkeypatch, capsys) -> None:
    monkeypatch.setattr("tally_automation.cli.get_settings", lambda: settings)
    findings = {f.name: f for f in ops.preflight(settings, contact_tally=False)}
    assert findings["database schema"].level == "ok"
    assert findings["user accounts"].level == "ok"            # the test client logged in as a synthetic admin
    assert findings["latest backup"].level == "warn"
    assert findings["AI extraction"].level == "warn"           # no provider in test settings
    assert cli(["check", "--no-tally"]) == 0
    ops.backup(settings)
    assert {f.name: f for f in ops.preflight(settings, contact_tally=False)}["latest backup"].level == "ok"

    with_key = settings.model_copy(update={"llm_provider": "groq", "llm_api_key": None})
    assert {f.name: f for f in ops.preflight(with_key, contact_tally=False)}["AI extraction"].level == "fail"
    printed = capsys.readouterr().out
    assert "Ready to run." in printed and "gsk_" not in printed


def test_preflight_fails_without_an_administrator(settings: Settings, migrated_engine) -> None:
    findings = {f.name: f for f in ops.preflight(settings, contact_tally=False)}
    assert findings["user accounts"].level == "fail"


def test_cli_backup_and_migrate(settings: Settings, migrated_engine, monkeypatch, capsys) -> None:
    monkeypatch.setattr("tally_automation.cli.get_settings", lambda: settings)
    assert cli(["migrate"]) == 0 and "already up to date" in capsys.readouterr().out
    assert cli(["backup"]) == 0 and "integrity ok" in capsys.readouterr().out
    assert ops.latest_backup(settings) > datetime.now(UTC) - timedelta(minutes=1)
    assert UserRole.ADMIN.value == "admin"
