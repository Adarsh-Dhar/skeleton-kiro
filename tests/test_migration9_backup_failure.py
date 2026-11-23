"""A failed backup aborts migration 9 and leaves the database file untouched.
Example count is 25 instead of the default 100: every example builds a SQLite file, migrates 8 versions and runs the
migration entry point, which costs tens of milliseconds each; the input space (a few rows, two failure modes) is small.
"""
from __future__ import annotations
import contextlib
import dataclasses
import io
import sqlite3
import tempfile
from pathlib import Path
from unittest import mock
from hypothesis import HealthCheck, given, settings, strategies as st
import db.backup as backup_mod
from db import create_db_engine
from db.migrations import migrate
from db.migrate import main
from db.migrations.versions import MIGRATIONS
rows_st = st.lists(st.tuples(st.text(max_size=100), st.integers(-(2**40), 2**40)), max_size=30)
class _SizeLiar:
    """Read-only source proxy that reports one page too many, so the backup size check fails."""
    def __init__(self, conn):
        self._c = conn
    def backup(self, target):
        return self._c.backup(target)
    def execute(self, sql, *a):
        cur = self._c.execute(sql, *a)
        if sql.strip().lower() == "pragma page_count":
            return self._c.execute("SELECT ? + 1", (cur.fetchone()[0],))
        return cur
    def close(self):
        self._c.close()
def _build_v8(path: Path, rows) -> None:
    e = create_db_engine(str(path))
    migrate(e, target=8)
    e.dispose()
    c = sqlite3.connect(str(path))
    c.execute("CREATE TABLE payload (id INTEGER PRIMARY KEY, s TEXT, n INTEGER)")
    c.executemany("INSERT INTO payload (s, n) VALUES (?, ?)", rows)
    c.commit()
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()
def _version(path: Path) -> int:
    c = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return c.execute("SELECT MAX(version) FROM app_schema_version").fetchone()[0]
    finally:
        c.close()
@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(rows=rows_st, mode=st.sampled_from(["unwritable", "size_mismatch"]))
def test_failed_backup_aborts_migration_without_changing_database(rows, mode):
    # Feature: f1-setup-lab-restoration, Property 5: A failed backup aborts the migration without changing the database
    # **Validates: Requirements 3.8**
    with tempfile.TemporaryDirectory() as d:
        db_dir = Path(d)
        db = db_dir / "trialforge.db"
        _build_v8(db, rows)
        before = db.read_bytes()
        assert _version(db) == 8
        # record whether any statement of migration 9 ran
        ran = {"n": 0}
        patched = []
        for m in MIGRATIONS:
            if m.version == 9:
                orig = m.apply
                def spy(conn, _orig=orig):
                    ran["n"] += 1
                    return _orig(conn)
                m = dataclasses.replace(m, apply=spy)
            patched.append(m)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch("db.migrations.versions.MIGRATIONS", patched))
            if mode == "unwritable":
                # a regular file where the backups directory must be created makes the backup unwritable
                (db_dir / "backups").write_bytes(b"not a directory")
            else:
                real = backup_mod._ro_connect
                stack.enter_context(mock.patch.object(backup_mod, "_ro_connect", lambda f: _SizeLiar(real(f))))
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            code = main(["--db", str(db)])
        assert code != 0
        assert "backup" in err.getvalue().lower()
        assert ran["n"] == 0
        assert db.read_bytes() == before
        assert _version(db) == 8
        # no finished backup file was left behind
        assert not list((db_dir / "backups").glob("*/*.db")) if (db_dir / "backups").is_dir() else True
