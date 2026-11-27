"""Migration 8 (Migration 9 then recreates the retired tables): F1 SETUP LAB keeps only OpenF1 historical data.

Its setup experiments (every SDK row of experiments owned by f1-setup-lab) and its retired tables are removed, together with
historical sessions and import jobs of other providers. WATTWISE LAB experiments and every OpenF1 session stay.
"""
from __future__ import annotations

import json

from sqlalchemy import text

from db import create_db_engine, current_version
from db.migrations import migrate
from db.migrations.game_data import GAME_TABLES

RETIRED = GAME_TABLES  # the tables migration 8 drops and migration 9 recreates


def _tables(engine) -> set[str]:
    with engine.connect() as c:
        return {r[0] for r in c.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'"))}


def _version7_database(path):
    """A database as it was before migration 8: SDK experiment tables with one F1 and one WATTWISE experiment, the retired F1
    tables, an OpenF1 and a FastF1 session, and import jobs of both providers."""
    e = create_db_engine(path)
    migrate(e, target=7)
    with e.begin() as c:
        c.execute(text("CREATE TABLE experiments (experiment_id VARCHAR(100) PRIMARY KEY, name VARCHAR(300))"))
        c.execute(text("CREATE TABLE runs (run_id VARCHAR(100) PRIMARY KEY, experiment_id VARCHAR(100) REFERENCES experiments(experiment_id))"))
        c.execute(text("CREATE TABLE state_log (seq INTEGER PRIMARY KEY, experiment_id VARCHAR(100), run_id VARCHAR(100) REFERENCES runs(run_id))"))
        c.execute(text("CREATE TABLE plans (experiment_id VARCHAR(100) PRIMARY KEY REFERENCES experiments(experiment_id), plan_json TEXT)"))
        for eid, app in (("f1-exp", "f1-setup-lab"), ("ww-exp", "wattwise-lab")):
            c.execute(text("INSERT INTO experiments VALUES (:e, 'x')"), {"e": eid})
            c.execute(text("INSERT INTO plans VALUES (:e, '{}')"), {"e": eid})
            for i in range(3):
                c.execute(text("INSERT INTO runs VALUES (:r, :e)"), {"r": f"{eid}-r{i}", "e": eid})
                c.execute(text("INSERT INTO state_log (experiment_id, run_id) VALUES (:e, :r)"), {"r": f"{eid}-r{i}", "e": eid})
            c.execute(text("INSERT INTO app_experiment_owner (experiment_id, app, created_at) VALUES (:e, :a, CURRENT_TIMESTAMP)"), {"e": eid, "a": app})
        for t in RETIRED:
            c.execute(text(f'CREATE TABLE IF NOT EXISTS "{t}" (id INTEGER PRIMARY KEY)'))
            c.execute(text(f'INSERT INTO "{t}" (id) VALUES (1)'))
        for sid, provider in (("openf1-2025-x-r", "openf1"), ("fastf1-2024-y-q", "fastf1")):
            c.execute(text("INSERT INTO f1_hist_session (session_id, status, provider, manifest_json, imported_at) "
                           "VALUES (:s, 'complete', :p, '{}', CURRENT_TIMESTAMP)"), {"s": sid, "p": provider})
            c.execute(text("INSERT INTO f1_hist_frame (session_id, kind, driver, columns_json, rows) VALUES (:s, 'laps', '', '[]', 0)"), {"s": sid})
        for jid, request in (("j-open", {"provider": "openf1", "year": 2025}), ("j-fast", {"provider": "fastf1", "year": 2024}),
                             ("j-old", {"year": 2024})):  # requests from before providers existed were FastF1
            c.execute(text("INSERT INTO f1_import_job (import_id, state, detail, request_json, dedupe_key, attempts, cancel_requested, "
                           "created_at, updated_at) VALUES (:i, 'COMPLETED', '', :r, :i, 1, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"),
                      {"i": jid, "r": json.dumps(request)})
    return e


def test_f1_experiments_and_retired_tables_go_while_wattwise_and_openf1_stay(tmp_path):
    e = _version7_database(tmp_path / "t.db")
    assert current_version(e) == 7
    migrate(e, target=9)
    assert current_version(e) == 9
    assert set(RETIRED) <= _tables(e)  # migration 9 recreates the tables migration 8 dropped
    with e.connect() as c:
        q = lambda sql: c.execute(text(sql)).all()  # noqa: E731
        assert q("SELECT experiment_id FROM experiments") == [("ww-exp",)]
        assert {r for (r,) in q("SELECT run_id FROM runs")} == {"ww-exp-r0", "ww-exp-r1", "ww-exp-r2"}
        assert q("SELECT DISTINCT experiment_id FROM state_log") == [("ww-exp",)] and q("SELECT experiment_id FROM plans") == [("ww-exp",)]
        assert q("SELECT experiment_id, app FROM app_experiment_owner") == [("ww-exp", "wattwise-lab")]
        assert q("SELECT session_id FROM f1_hist_session") == [("openf1-2025-x-r",)]
        assert q("SELECT session_id FROM f1_hist_frame") == [("openf1-2025-x-r",)]  # the FastF1 session's rows cascaded away
        assert q("SELECT import_id FROM f1_import_job") == [("j-open",)]
        assert c.execute(text("PRAGMA foreign_key_check")).all() == []


def test_a_fresh_database_reaches_the_latest_version_without_the_retired_tables(tmp_path):
    e = create_db_engine(tmp_path / "fresh.db")
    migrate(e)
    assert current_version(e) == 12 and not set(RETIRED) & _tables(e)  # migration 9 recreates them, migration 10 drops them
    assert migrate(e) == []  # idempotent
