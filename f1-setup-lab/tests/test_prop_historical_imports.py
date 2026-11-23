"""Property 12: a historical import writes only historical tables, even when it fails part-way.

Each example starts the real app on a temporary root database, puts setup data in every non-historical table, imports one real
OpenF1 fixture session (tests/fixtures/openf1) through the HTTP API with a failure injected at a random point, and compares
snapshots of all setup tables (six Restored_Tables, SDK tables, app_experiment_owner, ww_*) before and after.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings, strategies as st
from sqlalchemy import event
from sqlalchemy.engine import Engine

from f1lab.api.app import create_app
from f1lab.integrations.historical import repository as R
from f1lab.integrations.historical.openf1_client import OpenF1Client, RateLimiter

from db.guard import HISTORICAL_TABLES, RESTORED_TABLES, SETUP_SDK_TABLES, target_table

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import gen_historical_manifest as gen  # noqa: E402

SESSIONS = sorted(p.stem for p in gen.FIXTURES.glob("*.json"))
TERMINAL = ("COMPLETED", "FAILED", "CANCELLED")
SETUP_TABLES = SETUP_SDK_TABLES | RESTORED_TABLES | {"app_experiment_owner", "ww_household", "ww_tariff", "ww_meter_import", "ww_meter_reading"}
SEEDS = {  # one INSERT per table; ``{n}`` makes the key unique per seeded row
    "app_experiment_owner": "INSERT INTO app_experiment_owner (experiment_id, app, created_at) VALUES ('seed-{n}', 'wattwise-lab', '2026-01-01')",
    "f1_udp_recording": "INSERT INTO f1_udp_recording (recording_id, started_at, source) VALUES ('rec-{n}', '2026-01-01', 'seed')",
    "f1_udp_packet": "INSERT INTO f1_udp_packet (recording_id, seq, received_at, data) VALUES ('rec-{n}', {n}, 1.5, x'0102')",
    "f1_stint_evidence": "INSERT INTO f1_stint_evidence (experiment_id, run_id, payload_json, recorded_at) VALUES ('e{n}', 'r{n}', '{{}}', '2026-01-01')",
    "f1_stint_telemetry": "INSERT INTO f1_stint_telemetry (experiment_id, run_id, seq, sample_json) VALUES ('e{n}', 'r{n}', {n}, '{{}}')",
    "f1_probe_report": "INSERT INTO f1_probe_report (report_id, created_at, report_json) VALUES ('p{n}', '2026-01-01', '{{}}')",
    "f1_setup_mapping_confirmation": "INSERT INTO f1_setup_mapping_confirmation (factor, game_variant, packet_field, verified, confirmed_by, confirmed_at) "
                                     "VALUES ('f{n}', 'v', 'pf', 1, 'seed', '2026-01-01')",
    "ww_household": "INSERT INTO ww_household (id, settings_json, source, updated_at) VALUES ({n}, '{{}}', 'seed', '2026-01-01')",
    "ww_tariff": "INSERT INTO ww_tariff (tariff_id, name, position, data_json, created_at, updated_at) VALUES ('t{n}', 'T', {n}, '{{}}', '2026-01-01', '2026-01-01')",
    "ww_meter_import": "INSERT INTO ww_meter_import (import_id, file_name, imported_at, source, timezone, reading_kind, unit, rows_total, rows_used, rows_skipped, "
                       "content_sha256, options_json, warnings_json, row_log_json, gaps_json) VALUES ('m{n}', 'f.csv', '2026-01-01', 's', 'UTC', 'power', 'kW', 1, 1, 0, "
                       "'x', '{{}}', '[]', '[]', '[]')",
    "ww_meter_reading": "INSERT INTO ww_meter_reading (import_id, seq, ts_utc, interval_minutes, power_kw, energy_kwh) VALUES ('m{n}', {n}, '2026-01-01T00:00', 15, 1.0, 0.25)",
}


def _seed(path: Path, rows: int) -> None:
    with sqlite3.connect(path) as c:
        c.execute("PRAGMA foreign_keys=ON")
        for n in range(1, rows + 1):  # parents come first in SEEDS, so foreign keys hold
            for sql in SEEDS.values():
                c.execute(sql.format(n=n))


def _snapshot(path: Path) -> dict[str, list]:
    """Every table outside the historical domain (plus the migration ledger), with all its rows."""
    c = sqlite3.connect(path)
    try:
        names = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {n: sorted(map(repr, c.execute(f'SELECT * FROM "{n}"').fetchall())) for n in names if n not in HISTORICAL_TABLES}
    finally:
        c.close()


def _historical_rows(path: Path) -> dict[str, int]:
    c = sqlite3.connect(path)
    try:
        return {t: c.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in sorted(HISTORICAL_TABLES) if t != "f1_import_job"}
    finally:
        c.close()


injection = st.one_of(
    st.just(("none", 0)),
    st.tuples(st.just("http_error"), st.integers(0, 60)),  # the k-th OpenF1 request is answered 403 (not retried by the client)
    st.tuples(st.just("cancel"), st.integers(0, 60)),  # a cancel is requested while the k-th OpenF1 request is made
    st.tuples(st.just("write_error"), st.integers(0, 12)),  # the k-th frame write inside the save transaction raises
)


# Feature: f1-setup-lab-restoration, Property 12: Historical imports touch only historical tables, even on failure
# **Validates: Requirements 6.2, 6.3, 6.7**
# Example count is 30, below the usual 100: every example starts the whole app, seeds the database and runs a real fixture import
# (seconds each), and the input space is small (5 fixtures x 4 failure kinds x a few dozen request/write positions).
@settings(max_examples=30, deadline=None, suppress_health_check=list(HealthCheck))
@given(label=st.sampled_from(SESSIONS), inj=injection, seed_rows=st.integers(1, 3))
def test_historical_imports_touch_only_historical_tables_even_on_failure(label, inj, seed_rows):
    kind, k = inj
    d = json.loads((gen.FIXTURES / f"{label}.json").read_text())
    sess, meeting = d["sessions"][0], d["meetings"][0]
    year, event_name, code = int(sess["year"]), meeting["meeting_name"], gen.CODES[sess["session_name"]]
    driver = next(x["name_acronym"] for x in d["drivers"] if str(x["driver_number"]) == str(d["_meta"]["driver_number"]))

    written: list[str] = []
    recording = threading.Event()

    def recorder(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        if recording.is_set() and (t := target_table(statement)):
            written.append(t)

    with tempfile.TemporaryDirectory(prefix="prop12-") as tmp:
        tmp_path = Path(tmp)
        db = tmp_path / "trialforge.db"
        base_fetch = gen.fixture_fetch(d)
        calls = {"n": 0}
        lock = threading.Lock()

        def fetch(url, timeout):
            with lock:
                i, calls["n"] = calls["n"], calls["n"] + 1
            if i == k and kind == "http_error":
                return 403, {}, b'{"detail":"injected failure"}'
            if i == k and kind == "cancel":
                with sqlite3.connect(db, timeout=30) as c:
                    c.execute("UPDATE f1_import_job SET cancel_requested=1")
            return base_fetch(url, timeout)

        client = OpenF1Client(tmp_path / "cache", fetch=fetch, limiter=RateLimiter(per_second=10_000, per_minute=10_000_000), sleep=lambda s: None)
        app = create_app(str(db), frontend_dir=str(tmp_path / "no-frontend"), openf1_client_factory=lambda: client)

        real_write, writes = R.write_frame, {"n": 0}

        def write_frame(*a, **kw):
            i, writes["n"] = writes["n"], writes["n"] + 1
            if kind == "write_error" and i == k:
                raise RuntimeError("injected write failure")
            return real_write(*a, **kw)

        event.listen(Engine, "before_cursor_execute", recorder)
        try:
            with TestClient(app) as http, mock.patch.object(R, "write_frame", write_frame):
                r = http.post("/api/experiments", json={"name": "seed", "experiment_id": "seed-exp", "simulator": {"latency_seconds": 0.0}})
                assert r.status_code == 201, r.text
                _seed(db, seed_rows)
                before = _snapshot(db)
                assert SETUP_TABLES <= set(before)
                assert all(before[t] for t in SEEDS), "every seeded table has data"

                recording.set()
                pre = "/api/historical"
                iid = http.post(f"{pre}/import", json={"year": year, "event": event_name, "session": code, "drivers": [driver]}).json()["import_id"]
                st_: dict = {}
                for _ in range(1200):
                    st_ = http.get(f"{pre}/imports/{iid}").json()
                    if st_["state"] in TERMINAL:
                        break
                    time.sleep(0.05)
                recording.clear()
                assert st_["state"] in TERMINAL, st_
        finally:
            event.remove(Engine, "before_cursor_execute", recorder)

        # 6.2 / 6.7: setup rows are exactly as before, and every statement that wrote anything targeted a historical table
        assert _snapshot(db) == before
        assert set(written) <= HISTORICAL_TABLES, sorted(set(written) - HISTORICAL_TABLES)
        assert "f1_import_job" in written

        # 6.3: a failed or cancelled import is recorded in f1_import_job and leaves no historical session rows either
        if kind == "write_error" and writes["n"] > k:
            assert st_["state"] == "FAILED" and st_["error_code"]
        if kind == "cancel" and calls["n"] > k:
            assert st_["state"] in ("CANCELLED", "COMPLETED")
        if st_["state"] != "COMPLETED":
            assert st_["error"] or st_["error_code"]
            assert all(n == 0 for n in _historical_rows(db).values())
        else:
            assert _historical_rows(db)["f1_hist_session"] == 1
