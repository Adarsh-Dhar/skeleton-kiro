"""The F1 SETUP LAB game-recording tables: how they were created (migrations 8 and 9) and how they were removed (migration 10).

Migration 10 removes the "Live Simulator" feature (stints recorded from the EA SPORTS F1 25 game over UDP). Its tables no longer
have SQLAlchemy models, so migration 9 creates them from the frozen DDL below, exactly as the models defined them when migration 9
was released. Existing databases are unaffected by that; a fresh database creates the tables at migration 9 and drops them again
at migration 10.
"""
from __future__ import annotations

import json
import logging

from sqlalchemy import Connection, text

log = logging.getLogger("db.migrations")

# children before parents
GAME_TABLES = (
    "f1_udp_packet", "f1_udp_recording", "f1_stint_telemetry", "f1_stint_evidence", "f1_probe_report", "f1_setup_mapping_confirmation",
)  # fmt: skip

# SDK tables that hold per-experiment rows (the SDK creates them on first use, so any of them may be missing)
SDK_EXPERIMENT_TABLES = ("state_log", "jobs", "processed_measurements", "measurements", "analyses", "recommendations", "reports",
                         "plans", "runs", "experiments")  # children before parents (foreign keys are enforced)  # fmt: skip

GAME_MODE = "live_simulator"

DDL: dict[str, str] = {  # parents first
    "f1_udp_recording": """CREATE TABLE IF NOT EXISTS f1_udp_recording (
        recording_id VARCHAR(60) NOT NULL, started_at DATETIME NOT NULL, host VARCHAR(100), port INTEGER,
        source VARCHAR(40) NOT NULL, note TEXT, CONSTRAINT pk_f1_udp_recording PRIMARY KEY (recording_id))""",
    "f1_udp_packet": """CREATE TABLE IF NOT EXISTS f1_udp_packet (
        recording_id VARCHAR(60) NOT NULL, seq INTEGER NOT NULL, received_at FLOAT NOT NULL, data BLOB NOT NULL,
        CONSTRAINT pk_f1_udp_packet PRIMARY KEY (recording_id, seq),
        CONSTRAINT fk_f1_udp_packet_recording_id_f1_udp_recording FOREIGN KEY(recording_id)
            REFERENCES f1_udp_recording (recording_id) ON DELETE CASCADE)""",
    "f1_stint_evidence": """CREATE TABLE IF NOT EXISTS f1_stint_evidence (
        experiment_id VARCHAR(100) NOT NULL, run_id VARCHAR(100) NOT NULL, payload_json TEXT NOT NULL, recorded_at DATETIME NOT NULL,
        CONSTRAINT pk_f1_stint_evidence PRIMARY KEY (experiment_id, run_id))""",
    "f1_stint_telemetry": """CREATE TABLE IF NOT EXISTS f1_stint_telemetry (
        experiment_id VARCHAR(100) NOT NULL, run_id VARCHAR(100) NOT NULL, seq INTEGER NOT NULL, session_time FLOAT,
        sample_json TEXT NOT NULL, CONSTRAINT pk_f1_stint_telemetry PRIMARY KEY (experiment_id, run_id, seq))""",
    "f1_probe_report": """CREATE TABLE IF NOT EXISTS f1_probe_report (
        report_id VARCHAR(60) NOT NULL, created_at DATETIME NOT NULL, packet_format INTEGER, game_variant VARCHAR(40),
        recording_id VARCHAR(60), report_json TEXT NOT NULL, CONSTRAINT pk_f1_probe_report PRIMARY KEY (report_id))""",
    "f1_setup_mapping_confirmation": """CREATE TABLE IF NOT EXISTS f1_setup_mapping_confirmation (
        id INTEGER NOT NULL, factor VARCHAR(60) NOT NULL, game_variant VARCHAR(40) NOT NULL, packet_field VARCHAR(60) NOT NULL,
        verified BOOLEAN NOT NULL, low_value_seen FLOAT, high_value_seen FLOAT, probe_report_id VARCHAR(60),
        confirmed_by VARCHAR(100) NOT NULL, note TEXT, confirmed_at DATETIME NOT NULL, CONSTRAINT pk_f1_setup_mapping_confirmation PRIMARY KEY (id),
        CONSTRAINT uq_f1_setup_mapping_confirmation_factor_variant UNIQUE (factor, game_variant))""",
}  # fmt: skip

# What the last run of migration 10 removed, for the caller that wants to report it: {"experiments": n, "<table>": rows, ...}
LAST_REMOVAL: dict[str, int] = {}


def create_game_tables(conn: Connection, *names: str) -> None:
    """Migration 9: create the named game tables (all six, parents first, when none is named). Idempotent."""
    for name in names or DDL:
        try:
            conn.exec_driver_sql(DDL[name])
        except Exception as exc:
            raise RuntimeError(f"creating table {name} failed: {exc}") from exc


def _tables(conn: Connection) -> set[str]:
    return {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'"))}


def remove_game_data(conn: Connection) -> dict[str, int]:
    """Migration 10: delete every Live Simulator experiment (and all its SDK rows), then drop the game tables.

    An experiment is a Live Simulator experiment when its stored spec has ``metadata.mode == "live_simulator"`` (or a
    ``metadata.game`` entry), or when stint evidence / decoded telemetry exists for it. Synthetic experiments, historical
    ``f1_hist_*`` / ``f1_import_job`` data and WATTWISE data are not touched. Returns the number of rows removed per table.
    """
    existing = _tables(conn)
    removed: dict[str, int] = {}
    ids: set[str] = set()
    if "experiments" in existing:
        for eid, spec in conn.execute(text("SELECT experiment_id, spec_json FROM experiments")).all():
            try:
                meta = (json.loads(spec) or {}).get("metadata") or {}
            except (TypeError, ValueError):
                continue
            if meta.get("mode") == GAME_MODE or meta.get("game"):
                ids.add(eid)
    for table in ("f1_stint_evidence", "f1_stint_telemetry"):
        if table in existing:
            ids.update(r[0] for r in conn.execute(text(f'SELECT DISTINCT experiment_id FROM "{table}"')))
    for table in SDK_EXPERIMENT_TABLES:
        if table in existing:
            removed[table] = sum(conn.execute(text(f'DELETE FROM "{table}" WHERE experiment_id = :e'), {"e": e}).rowcount for e in ids)
    if "app_experiment_owner" in existing:
        removed["app_experiment_owner"] = sum(
            conn.execute(text("DELETE FROM app_experiment_owner WHERE experiment_id = :e"), {"e": e}).rowcount for e in ids)
    for table in GAME_TABLES:
        if table in existing:
            removed[table] = conn.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar_one()
            conn.execute(text(f'DROP TABLE "{table}"'))
    removed["experiments_removed"] = len(ids)
    LAST_REMOVAL.clear()
    LAST_REMOVAL.update(removed)
    log.info("migration 10 removed Live Simulator data: %s", removed)
    return removed
