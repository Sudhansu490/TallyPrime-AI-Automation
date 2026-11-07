"""Deployment operations (Phase 10): schema migration, backups, and a pre-flight check.

* ``migrate`` – bring the database schema to the latest version (Alembic).
* ``schema_status`` – whether the database is at the latest version. The server
  refuses to start on an outdated schema instead of failing later.
* ``backup`` – a consistent online copy of the SQLite database (SQLite's backup
  API, safe while the app runs), checked with ``PRAGMA integrity_check``, plus
  the stored original documents (content-addressed, so only new files are
  copied). The newest ``BACKUP_KEEP`` database copies are kept. ``.env`` is not
  backed up: it holds secrets and is recreated from ``.env.example``.
* ``preflight`` – checks a deployment before it is trusted: configuration,
  schema, accounts, directories, bind addresses, AI provider, TallyPrime
  (read-only), write switch and backups. Prints facts, never secrets.
"""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import make_url

from tally_automation.config import PROJECT_ROOT, Settings

BACKUP_PREFIX = "tally_automation-"


def alembic_config(settings: Settings) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["database_url"] = settings.database_url
    config.attributes["configure_logger"] = False
    return config


def schema_status(settings: Settings) -> tuple[str | None, str]:
    """(current revision of the database, latest revision in the code)."""
    head = ScriptDirectory.from_config(alembic_config(settings)).get_current_head()
    engine = create_engine(settings.database_url)
    try:
        with engine.connect() as connection:
            current = MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()
    assert head is not None
    return current, head


def migrate(settings: Settings) -> tuple[str | None, str]:
    before, _ = schema_status(settings)
    command.upgrade(alembic_config(settings), "head")
    return before, schema_status(settings)[0] or ""


def _database_path(settings: Settings) -> Path:
    url = make_url(settings.database_url)
    if url.get_backend_name() != "sqlite" or not url.database:
        raise ValueError("backups support the SQLite database only")
    return Path(url.database)


@dataclass
class BackupResult:
    database_copy: Path
    size_bytes: int
    documents_copied: int
    documents_total: int
    removed_old: int


def backup(settings: Settings, *, now: datetime | None = None) -> BackupResult:
    now = now or datetime.now(UTC)
    target_dir = settings.backup_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{BACKUP_PREFIX}{now:%Y%m%d-%H%M%S}.db"
    partial = target.with_suffix(".db.partial")
    source = sqlite3.connect(_database_path(settings))
    destination = sqlite3.connect(partial)
    try:
        source.backup(destination)          # consistent snapshot, even while the app writes
        result = destination.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        destination.close()
        source.close()
    if result != "ok":
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"The backup copy failed SQLite's integrity check ({result}); nothing was kept.")
    partial.replace(target)

    copied = total = 0
    uploads = settings.upload_dir
    if uploads.exists():
        mirror = target_dir / "uploads"
        for path in uploads.rglob("*"):
            if not path.is_file():
                continue
            total += 1
            destination_file = mirror / path.relative_to(uploads)
            # Files are named by their SHA-256: an existing copy is identical.
            if not destination_file.exists():
                destination_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination_file)
                copied += 1

    copies = sorted(target_dir.glob(f"{BACKUP_PREFIX}*.db"))
    old = copies[:-settings.backup_keep] if len(copies) > settings.backup_keep else []
    for path in old:
        path.unlink()
    return BackupResult(target, target.stat().st_size, copied, total, len(old))


def latest_backup(settings: Settings) -> datetime | None:
    copies = sorted(settings.backup_dir.glob(f"{BACKUP_PREFIX}*.db")) if settings.backup_dir.exists() else []
    if not copies:
        return None
    return datetime.fromtimestamp(copies[-1].stat().st_mtime, UTC)


# ---- Pre-flight check ----------------------------------------------------------------------------

@dataclass
class Finding:
    level: str          # "ok" | "warn" | "fail"
    name: str
    detail: str


def preflight(settings: Settings, *, contact_tally: bool = True) -> list[Finding]:
    from tally_automation.db.models import AppUser, UserRole
    from tally_automation.server import BindHostError, validate_bind_host

    out: list[Finding] = []
    add = lambda level, name, detail: out.append(Finding(level, name, detail))  # noqa: E731

    add("ok" if settings.app_env.value == "production" else "warn", "environment",
        f"APP_ENV={settings.app_env.value}" + ("" if settings.app_env.value == "production"
                                                else " (set APP_ENV=production on Computer A: hides /docs)"))
    try:
        current, head = schema_status(settings)
        add("ok" if current == head else "fail", "database schema",
            "up to date" if current == head else f"at {current or 'empty'}, code expects {head}: run 'migrate'")
    except Exception as exc:  # noqa: BLE001
        add("fail", "database", f"cannot be opened ({type(exc).__name__})")
        return out

    engine = create_engine(settings.database_url)
    try:
        with engine.connect() as connection:
            active = connection.execute(select(func.count()).select_from(AppUser.__table__)
                                        .where(AppUser.__table__.c.is_active.is_(True))).scalar_one()
            admins = connection.execute(select(func.count()).select_from(AppUser.__table__).where(
                AppUser.__table__.c.is_active.is_(True), AppUser.__table__.c.role == UserRole.ADMIN.value)).scalar_one()
    except Exception:  # noqa: BLE001 - schema not migrated
        active = admins = 0
    finally:
        engine.dispose()
    add("ok" if admins else "fail", "user accounts",
        f"{active} active, {admins} admin" if admins else
        "no active administrator: python -m tally_automation users create NAME --role admin")

    for directory in (settings.data_dir, settings.upload_dir, settings.log_dir, settings.backup_dir):
        try:
            directory.mkdir(parents=True, exist_ok=True)
            probe = directory / ".write-test"
            probe.write_text("ok")
            probe.unlink()
            add("ok", f"directory {directory.name}", "writable")
        except OSError as exc:
            add("fail", f"directory {directory.name}", f"not writable ({exc.strerror})")
    if settings.backup_dir.resolve().drive == settings.data_dir.resolve().drive:
        add("warn", "backup location", "on the same drive as the data; set BACKUP_DIR to another drive or disk")
    last = latest_backup(settings)
    add("ok" if last and datetime.now(UTC) - last < timedelta(days=2) else "warn", "latest backup",
        last.strftime("%Y-%m-%d %H:%M UTC") if last else "none yet: python -m tally_automation backup")

    for host in settings.bind_hosts:
        try:
            address = validate_bind_host(host)
            add("ok", "bind address", f"{address} ({'loopback' if address.is_loopback else 'Tailscale'})")
        except BindHostError as exc:
            add("fail", "bind address", str(exc))
    if all(h.startswith("127.") or h == "::1" for h in settings.bind_hosts):
        add("warn", "access from Computer B", "BIND_HOSTS is loopback only: Computer B cannot connect")

    if settings.llm_provider:
        has_key = settings.llm_api_key is not None and bool(settings.llm_api_key.get_secret_value())
        add("ok" if has_key else "fail", "AI extraction", f"{settings.llm_provider}, API key {'set' if has_key else 'MISSING'}")
    else:
        add("warn", "AI extraction", "disabled (LLM_PROVIDER empty): documents stop after text extraction")

    if not settings.tally_host:
        add("warn", "TallyPrime", "TALLY_HOST not set")
    elif contact_tally:
        from tally_automation.tally.client import TallyConfigError, TallyConnectionError, create_tally_client

        try:
            with create_tally_client(settings) as client:
                status = client.server_status()
                companies = client.companies() if status.running else []
            add("ok" if status.running else "warn", "TallyPrime",
                f"{settings.tally_host}:{settings.tally_port} answered in {status.milliseconds} ms; "
                f"{len(companies)} company(ies) open")
        except (TallyConfigError, TallyConnectionError) as exc:
            add("warn", "TallyPrime", f"not reachable now ({exc}); only needed for masters and imports")
        except Exception as exc:  # noqa: BLE001
            add("warn", "TallyPrime", f"unexpected answer ({type(exc).__name__})")
    add("ok" if not settings.tally_write_enabled else "warn", "TallyPrime writes",
        "disabled" if not settings.tally_write_enabled else "ENABLED: approved vouchers can be imported")
    return out
