"""Property test: setup operations write only to setup-domain tables and never touch historical or WATTWISE data."""
from __future__ import annotations

import itertools
import sqlite3

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings, strategies as st
from test_game_udp import BASE

from db.guard import HISTORICAL_TABLES, PERMITTED
from f1lab.api.app import create_app
from f1lab.catalogs import ea_f1_25
from f1lab.catalogs.verification import confirm, save_probe_report
from f1lab.integrations.game_udp.evidence import save_evidence, save_telemetry_series
from f1lab.integrations.game_udp.probe import SetupProbe
from f1lab.integrations.game_udp.recorder import RawRecorder
from f1lab.services.lab import LabService

SETUP = PERMITTED["setup"]
EXPECTED = {k: v["packet_field"] for k, v in ea_f1_25.GAME_FACTORS.items()}
CSV = "lap,sector_1,sector_2,sector_3\n1,28,35,27\n2,28.1,35.2,27.1\n"
_ids = itertools.count(1)


def snapshot(path) -> dict[str, list]:
    """Every table's rows, as sorted reprs (a read-only connection; WAL content is included)."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {n: sorted(repr(r) for r in con.execute(f'SELECT * FROM "{n}"')) for n in names}
    finally:
        con.close()


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    db = str(tmp_path_factory.mktemp("setupwrites") / "trialforge.db")
    lab = LabService(db)
    with TestClient(create_app(db, lab_factory=lambda: lab)) as client:
        con = sqlite3.connect(db)  # data owned by the other domains (outside the guarded engine), which must survive untouched
        con.execute("INSERT INTO ww_household (id, settings_json, source, updated_at) VALUES (1, '{}', 'user', '2026-01-01 00:00:00')")
        con.execute("INSERT INTO app_experiment_owner (experiment_id, app, created_at) VALUES ('ww-exp', 'wattwise-lab', '2026-01-01 00:00:00')")
        con.commit()
        con.close()
        yield client, lab, db, {"experiments": []}


def _wait(client, exp_id, timeout=60):
    import time

    end = time.time() + timeout
    while time.time() < end:
        c = client.get(f"/api/experiments/{exp_id}").json()["counts"]
        if c["queued"] + c["running"] == 0:
            return
        time.sleep(0.02)


def apply(op, env):
    client, lab, _db, state = env
    kind, n = op
    exps = state["experiments"]
    engine = lab.repo.db_engine
    if kind == "create" or (kind in ("run", "telemetry") and not exps):
        eid = f"prop-exp-{next(_ids)}"
        r = client.post("/api/experiments", json={"name": "p", "experiment_id": eid, "simulator": {"latency_seconds": 0.0}})
        assert r.status_code == 201, r.text
        exps.append(eid)
    if kind == "run":
        eid = exps[n % len(exps)]
        client.post(f"/api/experiments/{eid}/execute")
        _wait(client, eid)
    elif kind == "telemetry":
        eid = exps[n % len(exps)]
        runs = client.get(f"/api/experiments/{eid}/runs").json()
        if runs:
            r = client.post(f"/api/experiments/{eid}/runs/{runs[0]['run_id']}/telemetry", json={"csv": CSV})
            assert r.status_code < 500, r.text
    elif kind == "udp":
        rec = RawRecorder(engine, f"prop-rec-{next(_ids)}", source="probe")
        for i in range(1 + n % 4):
            rec.write(bytes([i, n % 256]) * 8, float(i))
        rec.close()
    elif kind == "evidence":
        eid, rid = f"prop-ev-{n % 3}", f"run-{n % 2}"
        save_evidence(engine, eid, rid, {"accepted": True, "n": n})
        save_telemetry_series(engine, eid, rid, [{"session_time": float(i), "speed": 300 + i} for i in range(1 + n % 5)])
    elif kind == "probe":
        p = SetupProbe(EXPECTED)
        p.observe(dict(BASE), 0, 2025)
        p.observe({**BASE, "front_wing": 1 + n % 5}, 1, 2025)
        rid = save_probe_report(engine, p.report())
        if n % 2:
            confirm(engine, "front_wing", "f1-25", verified=bool(n % 3), confirmed_by="prop", probe_report_id=rid)


ops = st.lists(st.tuples(st.sampled_from(["create", "run", "telemetry", "udp", "evidence", "probe"]), st.integers(0, 50)), min_size=1, max_size=5)


# Feature: f1-setup-lab-restoration, Property 11: Setup writes touch only setup tables
# **Validates: Requirements 6.1, 6.4**
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(seq=ops)
def test_setup_writes_touch_only_setup_tables(env, seq):
    db = env[2]
    before = snapshot(db)
    for op in seq:
        apply(op, env)
    after = snapshot(db)

    for t in HISTORICAL_TABLES | {n for n in before if n.startswith("ww_")}:
        assert after.get(t) == before.get(t), f"{t} changed"
    for t in set(before) | set(after):
        if before.get(t) != after.get(t):
            assert t in SETUP, f"setup operation {seq} changed non-setup table {t}"
    # app_experiment_owner: the WATTWISE row is untouched and every added row belongs to F1 SETUP LAB
    b, a = set(before["app_experiment_owner"]), set(after["app_experiment_owner"])
    assert b <= a, "an existing owner row was changed or removed"
    assert all("'f1-setup-lab'" in row for row in a - b), a - b
