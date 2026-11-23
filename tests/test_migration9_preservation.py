"""Property test for migration 9 (F1 SETUP LAB restoration): it preserves existing data and creates what is missing."""
from __future__ import annotations

import tempfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings, strategies as st
from sqlalchemy import text

from db import create_db_engine, current_version
from db.migrations import migrate
from db.migrations.game_data import DDL, create_game_tables

RESTORED = tuple(DDL)  # the six tables migration 9 creates, parents first

ids = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=1, max_size=12)


def _snapshot(conn) -> dict:
    """Every table's definition (sqlite_master rows for it and its indexes) and all of its rows."""
    out = {}
    names = [r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"))
             if r[0] != "app_schema_version"]  # the version table legitimately gains row 9
    for name in names:
        defs = conn.execute(text("SELECT type, name, sql FROM sqlite_master WHERE tbl_name = :n ORDER BY type, name"), {"n": name}).all()
        rows = sorted(map(repr, conn.execute(text(f'SELECT * FROM "{name}"')).all()))
        out[name] = (defs, rows)
    return out


@st.composite
def scenarios(draw):
    return {
        "sessions": draw(st.lists(ids, unique=True, max_size=4)),
        "jobs": draw(st.lists(ids, unique=True, max_size=4)),
        "tariffs": draw(st.lists(ids, unique=True, max_size=4)),
        "experiments": draw(st.lists(st.tuples(ids, st.sampled_from(["wattwise-lab", "f1-setup-lab"])), unique_by=lambda t: t[0], max_size=4)),
        "present": draw(st.sets(st.sampled_from(RESTORED))),
        "recordings": draw(st.lists(ids, unique=True, min_size=1, max_size=3)),
    }


def _populate(engine, s) -> None:
    with engine.begin() as c:
        c.execute(text("CREATE TABLE experiments (experiment_id VARCHAR(100) PRIMARY KEY, name VARCHAR(300))"))
        c.execute(text("CREATE TABLE runs (run_id VARCHAR(100) PRIMARY KEY, experiment_id VARCHAR(100) REFERENCES experiments(experiment_id))"))
        for sid in s["sessions"]:
            c.execute(text("INSERT INTO f1_hist_session (session_id, status, provider, manifest_json, imported_at) "
                           "VALUES (:s, 'complete', 'openf1', '{}', CURRENT_TIMESTAMP)"), {"s": sid})
            c.execute(text("INSERT INTO f1_hist_frame (session_id, kind, driver, columns_json, rows) VALUES (:s, 'laps', '', '[]', 0)"), {"s": sid})
        for jid in s["jobs"]:
            c.execute(text("INSERT INTO f1_import_job (import_id, state, detail, request_json, dedupe_key, attempts, cancel_requested, "
                           "created_at, updated_at) VALUES (:i, 'COMPLETED', '', '{\"provider\": \"openf1\"}', :i, 1, 0, "
                           "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"), {"i": jid})
        for tid in s["tariffs"]:
            c.execute(text("INSERT INTO ww_tariff (tariff_id, name, position, data_json, created_at, updated_at) "
                           "VALUES (:t, 'n', 0, '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"), {"t": tid})
        for eid, app in s["experiments"]:
            c.execute(text("INSERT INTO experiments VALUES (:e, 'x')"), {"e": eid})
            c.execute(text("INSERT INTO runs VALUES (:r, :e)"), {"r": f"{eid}-r", "e": eid})
            c.execute(text("INSERT INTO app_experiment_owner (experiment_id, app, created_at) VALUES (:e, :a, CURRENT_TIMESTAMP)"),
                      {"e": eid, "a": app})
        # any subset of the restored tables already present, with rows (parents first)
        create_game_tables(c, *[t for t in RESTORED if t in s["present"]])
        if "f1_udp_recording" in s["present"]:
            for r in s["recordings"]:
                c.execute(text("INSERT INTO f1_udp_recording (recording_id, started_at, source) VALUES (:r, CURRENT_TIMESTAMP, 'live')"), {"r": r})
                if "f1_udp_packet" in s["present"]:
                    c.execute(text("INSERT INTO f1_udp_packet (recording_id, seq, received_at, data) VALUES (:r, 0, 1.5, :d)"),
                              {"r": r, "d": b"\x00\x01"})
        if "f1_stint_evidence" in s["present"]:
            c.execute(text("INSERT INTO f1_stint_evidence (experiment_id, run_id, payload_json, recorded_at) "
                           "VALUES ('e', 'r', '{}', CURRENT_TIMESTAMP)"))
        if "f1_stint_telemetry" in s["present"]:
            c.execute(text("INSERT INTO f1_stint_telemetry (experiment_id, run_id, seq, session_time, sample_json) VALUES ('e', 'r', 0, 1.0, '{}')"))
        if "f1_probe_report" in s["present"]:
            c.execute(text("INSERT INTO f1_probe_report (report_id, created_at, report_json) VALUES ('p', CURRENT_TIMESTAMP, '{}')"))
        if "f1_setup_mapping_confirmation" in s["present"]:
            c.execute(text("INSERT INTO f1_setup_mapping_confirmation (factor, game_variant, packet_field, verified, confirmed_by, confirmed_at) "
                           "VALUES ('f', 'f1-25', 'p', 1, 'me', CURRENT_TIMESTAMP)"))


# Feature: f1-setup-lab-restoration, Property 1: Migration 9 preserves existing data and creates what is missing
# **Validates: Requirements 2.5, 3.2, 3.3, 3.4**
@settings(max_examples=100, deadline=None, suppress_health_check=list(HealthCheck))
@given(scenarios())
def test_migration_9_preserves_existing_data_and_creates_what_is_missing(s):
    with tempfile.TemporaryDirectory() as d:
        engine = create_db_engine(Path(d) / "t.db")
        try:
            migrate(engine, target=8)
            assert current_version(engine) == 8
            _populate(engine, s)
            with engine.connect() as c:
                before = _snapshot(c)
            migrate(engine, target=9)
            assert current_version(engine) == 9
            with engine.connect() as c:
                after = _snapshot(c)
        finally:
            engine.dispose()
    for name, state in before.items():
        assert after[name] == state, f"{name} changed"
    assert set(RESTORED) | {"app_experiment_owner"} <= set(after)
