"""Task 1: root db package foundation."""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from db import MigrationError, create_db_engine, current_version, default_db_path, latest_version, migrate, resolve_db_url
from db.migrations.versions import Migration

ROOT = Path(__file__).resolve().parent.parent


def test_default_path_is_repo_data_dir(monkeypatch):
    monkeypatch.delenv("TRIALFORGE_DB", raising=False)
    assert default_db_path() == ROOT / "data" / "trialforge.db"
    assert resolve_db_url().endswith("/data/trialforge.db")


def test_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("TRIALFORGE_DB", str(tmp_path / "x.db"))
    assert default_db_path() == (tmp_path / "x.db").resolve()
    assert resolve_db_url(None) == f"sqlite:///{(tmp_path / 'x.db').resolve()}"
    assert resolve_db_url("sqlite:///:memory:") == "sqlite:///:memory:"


def test_fresh_migrate_creates_schema_and_pragmas(tmp_path):
    e = create_db_engine(tmp_path / "sub" / "t.db")
    assert current_version(e) == 0
    applied = migrate(e)
    assert [m.version for m in applied] == list(range(1, latest_version() + 1))
    assert current_version(e) == latest_version()
    assert "app_experiment_owner" in inspect(e).get_table_names()
    with e.connect() as c:
        assert c.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert c.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert c.exec_driver_sql("PRAGMA busy_timeout").scalar() == 30000


def test_migrate_is_idempotent(tmp_path):
    e = create_db_engine(tmp_path / "t.db")
    migrate(e)
    assert migrate(e) == []
    assert current_version(e) == latest_version()


def test_failed_migration_rolls_back_everything(tmp_path):
    e = create_db_engine(tmp_path / "t.db")

    def ok(conn):
        conn.exec_driver_sql("CREATE TABLE a_ok (id INTEGER PRIMARY KEY)")

    def bad(conn):
        conn.exec_driver_sql("CREATE TABLE b_partial (id INTEGER PRIMARY KEY)")
        conn.exec_driver_sql("INSERT INTO b_partial VALUES (1)")
        raise RuntimeError("boom")

    ms = [Migration(1, "ok", ok), Migration(2, "bad", bad)]
    with pytest.raises(MigrationError, match="migration 2"):
        migrate(e, migrations=ms)
    tables = inspect(e).get_table_names()
    assert "a_ok" in tables  # migration 1 committed
    assert "b_partial" not in tables  # migration 2 left no partial DDL/DML
    assert current_version(e) == 1
    # a fixed migration 2 applies cleanly afterwards
    fixed = [ms[0], Migration(2, "fixed", lambda c: c.exec_driver_sql("CREATE TABLE b_partial (id INTEGER PRIMARY KEY)"))]
    assert [m.version for m in migrate(e, migrations=fixed)] == [2]


def test_duplicate_versions_rejected(tmp_path):
    e = create_db_engine(tmp_path / "t.db")
    with pytest.raises(MigrationError, match="duplicate"):
        migrate(e, migrations=[Migration(1, "a", lambda c: None), Migration(1, "b", lambda c: None)])


def test_transactions_roll_back_dml_and_ddl(tmp_path):
    e = create_db_engine(tmp_path / "t.db")
    with pytest.raises(RuntimeError):
        with e.begin() as c:
            c.exec_driver_sql("CREATE TABLE t (id INTEGER)")
            c.exec_driver_sql("INSERT INTO t VALUES (1)")
            raise RuntimeError
    assert "t" not in inspect(e).get_table_names()


WRITER = textwrap.dedent(
    """
    import sys
    from db import create_db_engine, migrate, immediate_transaction
    from sqlalchemy import text
    path, tag = sys.argv[1], sys.argv[2]
    e = create_db_engine(path)
    migrate(e)
    for i in range(60):
        with immediate_transaction(e) as c:
            c.execute(text("INSERT INTO app_experiment_owner (experiment_id, app, created_at) VALUES (:i, :a, CURRENT_TIMESTAMP)"),
                      {"i": f"{tag}-{i}", "a": tag})
    """
)


def test_two_processes_write_concurrently_without_losing_rows(tmp_path):
    path = str(tmp_path / "shared.db")
    cmd = lambda tag: [sys.executable, "-c", WRITER, path, tag]  # noqa: E731
    procs = [subprocess.Popen(cmd(t), cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for t in ("f1-setup-lab", "wattwise-lab")]
    outs = [p.communicate(timeout=120) for p in procs]
    for p, (_, err) in zip(procs, outs, strict=True):
        assert p.returncode == 0, err
    e = create_db_engine(path)
    with e.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM app_experiment_owner")).scalar_one() == 120
    assert current_version(e) == latest_version()


def test_cli_creates_database_and_reports_version(tmp_path):
    db = tmp_path / "cli.db"
    r = subprocess.run([sys.executable, "-m", "db.migrate", "--db", str(db)], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert db.exists()
    assert f"schema version: {latest_version()} (latest {latest_version()})" in r.stdout
    r2 = subprocess.run([sys.executable, "-m", "db.migrate", "--db", str(db), "--status"], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert "no pending" not in r2.stdout and "schema version" in r2.stdout
