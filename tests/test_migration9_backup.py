"""Migration 9 is preceded by a backup that equals the version-8 source.

Example count is 25 instead of the default 100: every example builds a SQLite file, migrates 8 versions and runs the
migration entry point twice, which costs tens of milliseconds each; the input space (a few rows of text/ints, WAL or not)
is small, so 25 examples cover it.
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings, strategies as st
from sqlalchemy import text

from db import create_db_engine, current_version
from db.migrations import migrate
from db.backup import BACKUP_FILE_NAME, backup_hook
from db.migrate import main
from db.migrations.versions import MIGRATIONS, Migration

rows_st = st.lists(
    st.tuples(st.text(max_size=200), st.integers(-(2**40), 2**40), st.binary(max_size=2000)),
    max_size=40,
)


def _build_v8(path: Path, rows, wal: bool):
    """Create a version-8 database; return a connection kept open so WAL content stays in the -wal file."""
    e = create_db_engine(str(path))
    migrate(e, target=8)
    e.dispose()
    keeper = sqlite3.connect(str(path))
    # the project's engine leaves the file in WAL mode, so select the mode explicitly
    keeper.execute(f"PRAGMA journal_mode={'WAL' if wal else 'DELETE'}")
    keeper.execute("CREATE TABLE payload (id INTEGER PRIMARY KEY, s TEXT, n INTEGER, b BLOB)")
    keeper.executemany("INSERT INTO payload (s, n, b) VALUES (?, ?, ?)", rows)
    keeper.commit()
    return keeper


def _dump(path: Path) -> dict[str, list]:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {n: conn.execute(f'SELECT * FROM "{n}" ORDER BY 1').fetchall() for n in names}
    finally:
        conn.close()


def _logical_size(path: Path) -> int:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return conn.execute("PRAGMA page_count").fetchone()[0] * conn.execute("PRAGMA page_size").fetchone()[0]
    finally:
        conn.close()


def _backups(db_dir: Path) -> list[Path]:
    return sorted((db_dir / "backups").glob(f"*/{BACKUP_FILE_NAME}"))


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(rows=rows_st, wal=st.booleans())
def test_backup_precedes_migration_9_and_equals_source(rows, wal):
    # Feature: f1-setup-lab-restoration, Property 4: Backup precedes migration 9 and equals the source
    # **Validates: Requirements 3.7**
    with tempfile.TemporaryDirectory() as d:
        db_dir = Path(d)
        db = db_dir / "trialforge.db"
        keeper = _build_v8(db, rows, wal)
        try:
            source_dump = _dump(db)
            source_size = _logical_size(db)
            if not wal:
                assert db.stat().st_size == source_size

            # 1) ordering: wrap migration 9 so it records whether the backup already existed at its first statement.
            seen: dict[str, object] = {}
            wrapped = []
            for m in MIGRATIONS:
                if m.version == 9:
                    def apply9(conn, _orig=m.apply):
                        seen["backups_at_start"] = _backups(db_dir)
                        return _orig(conn)
                    m = Migration(m.version, m.name, apply9)
                wrapped.append(m)
            engine = create_db_engine(str(db))
            try:
                applied = migrate(engine, target=9, migrations=wrapped, before_apply=backup_hook)
                assert [a.version for a in applied] == [9]
            finally:
                engine.dispose()
            assert len(seen["backups_at_start"]) == 1
            backup = seen["backups_at_start"][0]
            assert backup.parent.parent == db_dir / "backups"
            assert backup.name == "trialforge.before-migration-9.db"
            assert backup.stat().st_size == source_size
            assert _dump(backup) == source_dump

            # the migrated database moved on, the backup did not
            assert _dump(db) != source_dump
        finally:
            keeper.close()


@settings(max_examples=10, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(rows=rows_st, wal=st.booleans())
def test_entry_point_creates_equal_backup(rows, wal):
    # Feature: f1-setup-lab-restoration, Property 4: Backup precedes migration 9 and equals the source
    # **Validates: Requirements 3.7**
    with tempfile.TemporaryDirectory() as d:
        db_dir = Path(d)
        db = db_dir / "trialforge.db"
        keeper = _build_v8(db, rows, wal)
        try:
            source_dump = _dump(db)
            source_size = _logical_size(db)
            assert main(["--db", str(db)]) == 0
            found = _backups(db_dir)
            assert len(found) == 1
            assert found[0].stat().st_size == source_size
            assert _dump(found[0]) == source_dump
            engine = create_db_engine(str(db))
            try:
                assert current_version(engine) >= 9
            finally:
                engine.dispose()
        finally:
            keeper.close()
