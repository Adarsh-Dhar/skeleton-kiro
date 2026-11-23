"""Pre-migration backup of the root database.

Uses the SQLite online backup API on a read-only connection, so committed pages that still sit in the WAL file are
included and the source file is never modified. The copy is written to a temp file next to its final name and renamed
only after its size has been checked.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from db.config import sqlite_file_path

BACKUP_MIGRATION = 9
BACKUP_MIGRATIONS = (9, 10)  # migrations that are backed up first: 9 restored tables, 10 deletes the recorded-stint data
BACKUP_FILE_NAME = f"trialforge.before-migration-{BACKUP_MIGRATION}.db"


def backup_file_name(migration: int) -> str:
    return f"trialforge.before-migration-{migration}.db"

VERSION_TABLE = "app_schema_version"


class BackupError(RuntimeError):
    pass


def _ro_connect(db_file: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{db_file.resolve().as_uri()}?mode=ro", uri=True, timeout=30)


def backup_database(db_file: Path, now: datetime | None = None, migration: int = BACKUP_MIGRATION) -> Path:
    """Write ``<db dir>/backups/<YYYYmmddTHHMMSSZ>/trialforge.before-migration-<migration>.db`` and return its path.

    Raises ``BackupError`` if the copy cannot be written or its size differs from the source. The source is only opened
    read-only, so it is unchanged on failure.
    """
    db_file = Path(db_file)
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    target_dir = db_file.parent / "backups" / stamp
    name = backup_file_name(migration)
    final = target_dir / name
    tmp = target_dir / (name + ".tmp")
    src = dst = None
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        src = _ro_connect(db_file)
        dst = sqlite3.connect(str(tmp))
        src.backup(dst)
        pages = src.execute("PRAGMA page_count").fetchone()[0]
        page_size = src.execute("PRAGMA page_size").fetchone()[0]
        dst.close()
        dst = None
        expected = pages * page_size
        actual = tmp.stat().st_size
        if actual != expected:
            raise BackupError(f"backup size {actual} differs from source size {expected}")
        wal = Path(str(db_file) + "-wal")
        wal_pending = wal.exists() and wal.stat().st_size > 0
        if not wal_pending and db_file.stat().st_size != actual:
            raise BackupError(f"backup size {actual} differs from source file size {db_file.stat().st_size}")
        os.replace(tmp, final)
    except BackupError:
        _cleanup(tmp)
        raise
    except (OSError, sqlite3.Error) as exc:
        _cleanup(tmp)
        raise BackupError(f"could not back up {db_file} to {final}: {exc}") from exc
    finally:
        for c in (src, dst):
            if c is not None:
                try:
                    c.close()
                except sqlite3.Error:
                    pass
    return final


def _cleanup(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _stored_version(db_file: Path) -> int | None:
    """Highest applied version, or None when the file has no version table (never migrated)."""
    try:
        conn = _ro_connect(db_file)
    except sqlite3.Error as exc:
        raise BackupError(f"could not open {db_file} to read its schema version: {exc}") from exc
    try:
        has = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (VERSION_TABLE,)).fetchone()
        if not has:
            return None
        return int(conn.execute(f"SELECT COALESCE(MAX(version), 0) FROM {VERSION_TABLE}").fetchone()[0])
    except sqlite3.Error as exc:
        raise BackupError(f"could not read the schema version of {db_file}: {exc}") from exc
    finally:
        conn.close()


def backup_hook(engine, migration) -> Path | None:
    """``before_apply`` hook for ``migrate``: back up before migration 9 or 10 runs on an existing, migrated file database.

    Returns the backup path, or None when no backup is needed (other migration, in-memory or non-file database, missing
    or empty file, never-migrated database, or version already at or above the migration).
    """
    if migration.version not in BACKUP_MIGRATIONS:
        return None
    url = engine.url.render_as_string(hide_password=False)
    db_file = sqlite_file_path(url)
    if db_file is None:
        return None
    if not db_file.is_file() or db_file.stat().st_size == 0:
        return None
    version = _stored_version(db_file)
    if version is None or version >= migration.version:
        return None
    return backup_database(db_file, migration=migration.version)
