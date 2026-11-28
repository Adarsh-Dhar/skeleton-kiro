"""Migration 10 removes the Live Simulator data: its experiments (every SDK row), its owner rows and its six tables.

Synthetic experiments, historical ``f1_hist_*`` / ``f1_import_job`` data and WATTWISE data stay. The database is backed up first
(``db.backup.backup_hook``), like migration 9.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import text

from db import create_db_engine, current_version, latest_version
from db.backup import backup_hook
from db.migrations import migrate
from db.migrations.game_data import GAME_TABLES, LAST_REMOVAL

SDK = ("state_log", "jobs", "processed_measurements", "measurements", "analyses", "recommendations", "reports", "plans", "runs", "experiments")


def _tables(engine) -> set[str]:
    with engine.connect() as c:
        return {r[0] for r in c.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'"))}


def _sdk_tables(c) -> None:
    c.execute(text("CREATE TABLE experiments (experiment_id VARCHAR(100) PRIMARY KEY, name VARCHAR(300), spec_json TEXT)"))
    c.execute(text("CREATE TABLE runs (run_id VARCHAR(100) PRIMARY KEY, experiment_id VARCHAR(100) REFERENCES experiments(experiment_id))"))
    for t in ("plans", "analyses", "recommendations", "reports"):
        c.execute(text(f"CREATE TABLE {t} (experiment_id VARCHAR(100) REFERENCES experiments(experiment_id), payload_json TEXT)"))
    for t in ("measurements", "processed_measurements", "state_log", "jobs"):
        c.execute(text(f"CREATE TABLE {t} (experiment_id VARCHAR(100), run_id VARCHAR(100))"))


def _add_experiment(c, eid: str, app: str, mode: str | None) -> None:
    meta = {"app": app, "mode": mode} if mode else {"app": app, "synthetic": True}
    c.execute(text("INSERT INTO experiments VALUES (:e, 'x', :s)"), {"e": eid, "s": json.dumps({"metadata": meta})})
    c.execute(text("INSERT INTO app_experiment_owner (experiment_id, app, created_at) VALUES (:e, :a, CURRENT_TIMESTAMP)"), {"e": eid, "a": app})
    for t in ("plans", "analyses", "recommendations", "reports"):
        c.execute(text(f"INSERT INTO {t} VALUES (:e, '{{}}')"), {"e": eid})
    for i in range(2):
        r = f"{eid}-r{i}"
        c.execute(text("INSERT INTO runs VALUES (:r, :e)"), {"r": r, "e": eid})
        for t in ("measurements", "processed_measurements", "state_log", "jobs"):
            c.execute(text(f"INSERT INTO {t} VALUES (:e, :r)"), {"e": eid, "r": r})


def _v9_database(path: Path):
    """A version 9 database: one synthetic and one live-mode experiment of the F1 app, one WATTWISE experiment, game rows, historical rows."""
    e = create_db_engine(path)
    migrate(e, target=8)
    with e.begin() as c:
        _sdk_tables(c)
    migrate(e, target=9)
    with e.begin() as c:
        _add_experiment(c, "syn-1", "f1-setup-lab", None)
        _add_experiment(c, "live-1", "f1-setup-lab", "live_simulator")
        _add_experiment(c, "ww-1", "wattwise-lab", None)
        c.execute(text("INSERT INTO f1_udp_recording (recording_id, started_at, source) VALUES ('rec', CURRENT_TIMESTAMP, 'live')"))
        c.execute(text("INSERT INTO f1_udp_packet (recording_id, seq, received_at, data) VALUES ('rec', 0, 1.5, x'0102')"))
        c.execute(text("INSERT INTO f1_udp_packet (recording_id, seq, received_at, data) VALUES ('rec', 1, 1.6, x'0304')"))
        c.execute(text("INSERT INTO f1_stint_evidence (experiment_id, run_id, payload_json, recorded_at) VALUES ('live-1', 'live-1-r0', '{}', CURRENT_TIMESTAMP)"))
        c.execute(text("INSERT INTO f1_stint_telemetry (experiment_id, run_id, seq, sample_json) VALUES ('live-1', 'live-1-r0', 0, '{}')"))
        c.execute(text("INSERT INTO f1_probe_report (report_id, created_at, report_json) VALUES ('p', CURRENT_TIMESTAMP, '{}')"))
        c.execute(text("INSERT INTO f1_setup_mapping_confirmation (factor, game_variant, packet_field, verified, confirmed_by, confirmed_at) "
                       "VALUES ('front_wing', 'f1-25', 'front_wing', 1, 'me', CURRENT_TIMESTAMP)"))
        c.execute(text("INSERT INTO f1_hist_session (session_id, status, provider, manifest_json, imported_at) "
                       "VALUES ('openf1-2025-x-r', 'complete', 'openf1', '{}', CURRENT_TIMESTAMP)"))
        c.execute(text("INSERT INTO f1_hist_frame (session_id, kind, driver, columns_json, rows) VALUES ('openf1-2025-x-r', 'laps', '', '[]', 0)"))
        c.execute(text("INSERT INTO ww_tariff (tariff_id, name, position, data_json, created_at, updated_at) "
                       "VALUES ('t1', 'T', 0, '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
    return e


def _rows(e, sql):
    with e.connect() as c:
        return c.execute(text(sql)).all()


def test_migration_10_is_the_latest_and_follows_9():
    assert latest_version() == 15  # 13: bill reconciliation, 14: csv upload registry, 15: hist csv metadata


def test_game_experiments_rows_and_tables_are_removed_everything_else_stays(tmp_path):
    e = _v9_database(tmp_path / "t.db")
    assert current_version(e) == 9 and set(GAME_TABLES) <= _tables(e)
    migrate(e)
    assert current_version(e) == 15
    assert not set(GAME_TABLES) & _tables(e)
    # the game experiment is gone from every SDK table and from the owner table; the synthetic and WATTWISE ones are complete
    for t in SDK:
        assert _rows(e, f"SELECT COUNT(*) FROM {t} WHERE experiment_id = 'live-1'" if t != "runs" else "SELECT COUNT(*) FROM runs WHERE experiment_id = 'live-1'") == [(0,)], t
    assert sorted(_rows(e, "SELECT experiment_id, app FROM app_experiment_owner")) == [("syn-1", "f1-setup-lab"), ("ww-1", "wattwise-lab")]
    assert sorted(r[0] for r in _rows(e, "SELECT experiment_id FROM experiments")) == ["syn-1", "ww-1"]
    for eid in ("syn-1", "ww-1"):
        assert _rows(e, f"SELECT COUNT(*) FROM runs WHERE experiment_id = '{eid}'") == [(2,)]
        assert _rows(e, f"SELECT COUNT(*) FROM measurements WHERE experiment_id = '{eid}'") == [(2,)]
    assert _rows(e, "SELECT session_id FROM f1_hist_session") == [("openf1-2025-x-r",)]
    assert _rows(e, "SELECT session_id FROM f1_hist_frame") == [("openf1-2025-x-r",)]
    assert _rows(e, "SELECT tariff_id FROM ww_tariff") == [("t1",)]
    with e.connect() as c:
        assert c.execute(text("PRAGMA foreign_key_check")).all() == []
    assert migrate(e) == []


def test_removal_reports_the_rows_it_deleted(tmp_path):
    e = _v9_database(tmp_path / "t.db")
    migrate(e)
    assert LAST_REMOVAL["experiments_removed"] == 1
    assert LAST_REMOVAL["runs"] == 2 and LAST_REMOVAL["measurements"] == 2 and LAST_REMOVAL["app_experiment_owner"] == 1
    assert LAST_REMOVAL["f1_udp_recording"] == 1 and LAST_REMOVAL["f1_udp_packet"] == 2
    assert LAST_REMOVAL["f1_stint_evidence"] == 1 and LAST_REMOVAL["f1_stint_telemetry"] == 1
    assert LAST_REMOVAL["f1_probe_report"] == 1 and LAST_REMOVAL["f1_setup_mapping_confirmation"] == 1


def test_experiments_with_stint_evidence_are_removed_even_without_a_mode_in_the_spec(tmp_path):
    e = _v9_database(tmp_path / "t.db")
    with e.begin() as c:
        c.execute(text("UPDATE experiments SET spec_json = 'not json' WHERE experiment_id = 'live-1'"))
    migrate(e)
    assert sorted(r[0] for r in _rows(e, "SELECT experiment_id FROM experiments")) == ["syn-1", "ww-1"]


def test_a_fresh_database_and_one_without_sdk_tables_migrate(tmp_path):
    e = create_db_engine(tmp_path / "fresh.db")
    migrate(e)
    assert current_version(e) == 15 and not set(GAME_TABLES) & _tables(e)
    assert LAST_REMOVAL["experiments_removed"] == 0


def test_the_database_is_backed_up_before_migration_10(tmp_path):
    db = tmp_path / "trialforge.db"
    e = _v9_database(db)
    e.dispose()
    e = create_db_engine(db)
    before = sqlite3.connect(db).execute("SELECT COUNT(*) FROM f1_udp_packet").fetchone()[0]
    assert before == 2
    migrate(e, before_apply=backup_hook)
    backups = sorted((tmp_path / "backups").glob("*/trialforge.before-migration-10.db"))
    assert len(backups) == 1
    b = sqlite3.connect(backups[0])
    assert b.execute("SELECT MAX(version) FROM app_schema_version").fetchone()[0] == 9  # the copy is the pre-migration state
    assert b.execute("SELECT COUNT(*) FROM f1_udp_packet").fetchone()[0] == 2
    assert b.execute("SELECT experiment_id FROM experiments WHERE experiment_id = 'live-1'").fetchall() == [("live-1",)]
    b.close()
    assert current_version(e) == 15


def test_a_failing_removal_rolls_back_and_keeps_version_9(tmp_path):
    from unittest import mock

    from db.migrations import MigrationError
    e = _v9_database(tmp_path / "t.db")
    def boom(conn):  # deletes something, then fails: nothing of it may survive
        conn.execute(text("DELETE FROM runs"))
        raise RuntimeError("injected failure")

    with mock.patch("db.migrations.versions.remove_game_data", boom):
        with pytest.raises(MigrationError):
            migrate(e)
    assert current_version(e) == 9
    assert _rows(e, "SELECT COUNT(*) FROM experiments") == [(3,)]
    assert _rows(e, "SELECT COUNT(*) FROM f1_udp_packet") == [(2,)]
    assert _rows(e, "SELECT COUNT(*) FROM runs") == [(6,)]
