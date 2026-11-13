import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from flask import current_app


def get_database_path():
    database_url = current_app.config["SQLALCHEMY_DATABASE_URI"]
    if not database_url.startswith("sqlite://"):
        raise ValueError("Only SQLite database backups are supported in this prototype")
    return database_url.replace("sqlite:///", "", 1)


def create_backup():
    db_path = Path(get_database_path())
    if not db_path.exists():
        raise FileNotFoundError("Database file does not exist yet")
    backup_dir = Path(current_app.instance_path) / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = backup_dir / f"attendance_backup_{timestamp}.db"
    source = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    target = sqlite3.connect(str(backup_path))
    try:
        source.backup(target)
        result = target.execute("PRAGMA integrity_check").fetchone()
        if result is None or result[0] != "ok":
            raise RuntimeError("Backup integrity check failed")
        source_counts = {
            table: source.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for (table,) in source.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        }
        backup_counts = {
            table: target.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for (table,) in target.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        }
        if source_counts != backup_counts:
            raise RuntimeError("Backup verification did not match the source database")
    except Exception:
        target.close()
        source.close()
        backup_path.unlink(missing_ok=True)
        raise
    target.close()
    source.close()
    return str(backup_path)


def restore_backup(backup_path, confirm=False):
    if not confirm:
        raise ValueError("Pass confirm=True after reviewing the backup and creating a separate recovery copy.")
    backup = Path(backup_path)
    if not backup.exists():
        raise FileNotFoundError("Backup file not found")
    backup_root = (Path(current_app.instance_path) / "backups").resolve()
    if backup.resolve().parent != backup_root:
        raise ValueError("Restore source must be inside the protected backup directory")
    source = sqlite3.connect(f"file:{backup.resolve()}?mode=ro", uri=True)
    try:
        result = source.execute("PRAGMA integrity_check").fetchone()
        if result is None or result[0] != "ok":
            raise RuntimeError("Restore source failed its integrity check")
    finally:
        source.close()
    db_path = Path(get_database_path())
    safety_backup = create_backup() if db_path.exists() else None
    replacement = db_path.with_name(f"{db_path.name}.restore.tmp")
    source = sqlite3.connect(f"file:{backup.resolve()}?mode=ro", uri=True)
    target = sqlite3.connect(str(replacement))
    try:
        source.backup(target)
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Restore copy failed its integrity check")
    finally:
        target.close()
        source.close()
    from app.extensions import db

    db.engine.dispose()
    replacement.replace(db_path)
    return str(db_path)
