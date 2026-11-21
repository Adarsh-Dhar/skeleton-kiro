"""Task 10 (F1-K2): archived stint telemetry download, series index, and the inspector's data handling.

The browser part of the inspector is exercised in test_ui_browser.py (skipped without Chromium); here the inspector's
pure functions run under Node (skipped without node) and the endpoints run against a real root database.
"""
from __future__ import annotations

import csv
import io
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from test_live_mode import live_body

from f1lab.integrations.game_udp.evidence import load_telemetry_series, save_telemetry_series

FRONT = Path(__file__).resolve().parents[1] / "frontend"
SAMPLES = [{"session_time": 10.0 + i * 0.05, "frame_id": i, "packet_format": 2025, "packet_id": 6, "session_uid": 7, "speed_kph": 100.0 + i,
            "throttle": 0.5, "steer": 0.0, "brake": 0.0, "gear": 5, "rpm": 10000.0, "clutch": None, "drs": 0, "rev_lights_percent": None,
            "engine_temperature_c": None, "brake_temperature_c": [400, 401, 402, 403], "tyre_surface_temperature_c": [90, 91, 92, 93],
            "tyre_inner_temperature_c": [], "tyre_pressure_psi": [22.1, 22.2, 23.0, 23.1], "surface_type": [0, 0, 0, 0]} for i in range(250)]


@pytest.fixture
def live_run(client):
    r = client.post("/api/experiments", json=live_body())
    assert r.status_code == 201, r.text
    d = r.json()
    run_id = d["runs"][0]["run_id"]
    engine = client.app.state.lab.repo.db_engine
    save_telemetry_series(engine, d["id"], run_id, SAMPLES)
    return d, run_id, engine


def test_series_index_lists_stored_sample_counts(client, live_run):
    d, run_id, _ = live_run
    idx = client.get(f"/api/experiments/{d['id']}/telemetry/series-index").json()
    counts = {r["run_id"]: r["samples"] for r in idx}
    assert counts[run_id] == 250 and sum(counts.values()) == 250


def test_csv_download_has_every_measured_sample_and_wheel_columns(client, live_run):
    d, run_id, engine = live_run
    r = client.get(f"/api/experiments/{d['id']}/runs/{run_id}/telemetry/series.csv")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert len(rows) == 250  # not downsampled
    assert rows[0]["speed_kph"] == "100.0" and rows[-1]["speed_kph"] == "349.0"
    assert rows[0]["tyre_pressure_psi_fl"] == "23.0" and rows[0]["brake_temperature_c_rr"] == "401"
    assert rows[0]["clutch"] == "" and "tyre_inner_temperature_c_rl" not in rows[0]  # missing stays missing
    assert load_telemetry_series(engine, d["id"], run_id) == SAMPLES  # downloading never changes the stored data


def test_downloads_404_for_runs_without_series_and_synthetic_experiments(client, live_run, make_experiment):
    d, run_id, _ = live_run
    other = d["runs"][1]["run_id"]
    assert client.get(f"/api/experiments/{d['id']}/runs/{other}/telemetry/series.csv").status_code == 404
    syn = make_experiment()
    assert client.get(f"/api/experiments/{syn['id']}/telemetry/series-index").status_code == 409


def test_frontend_loads_the_inspector():
    html = (FRONT / "index.html").read_text()
    assert html.index("charts.js") < html.index("inspector.js") < html.index("app.js")
    js = (FRONT / "app.js").read_text()
    assert "Inspector.mount" in js and "telemetry/series.csv" in js


NODE_CHECK = r"""
const fs = require('fs'), vm = require('vm');
const ctx = { console };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2] + '/charts.js', 'utf8') + '\n' + fs.readFileSync(process.argv[2] + '/inspector.js', 'utf8') + '\nthis.Inspector = Inspector;', ctx);
const I = ctx.Inspector;
const samples = JSON.parse(fs.readFileSync(0, 'utf8'));
const original = JSON.stringify(samples);
const p = I.prepare([...samples].reverse());
const out = {
  sorted: p.every((s, i) => !i || s.session_time >= p[i - 1].session_time),
  distance: p.at(-1)._dist,
  untouched: JSON.stringify(samples) === original,
  available: I.availability(p),
  thinned: I.thin(p.map((s, i) => ({ x: i, y: i % 50 === 0 ? 999 : s.speed_kph })), 100),
};
out.thinnedLen = out.thinned.length; out.keepsPeak = out.thinned.some((q) => q.y === 999); delete out.thinned;
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_inspector_logic_under_node(tmp_path):
    script = tmp_path / "check.js"
    script.write_text(NODE_CHECK)
    r = subprocess.run(["node", str(script), str(FRONT)], input=json.dumps(SAMPLES), capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["sorted"] and out["untouched"]
    # trapezoid integration of 100..349 km/h at 0.05 s steps
    expected = sum(((100 + i) + (101 + i)) / 2 / 3.6 * 0.05 for i in range(249))
    assert out["distance"] == pytest.approx(expected, rel=1e-9)
    av = out["available"]
    assert av["speed_kph"] and av["tyre_pressure_psi"] and av["brake_temperature_c"]
    assert not av["clutch"] and not av["rev_lights_percent"] and not av["engine_temperature_c"] and not av["tyre_inner_temperature_c"]
    assert out["thinnedLen"] <= 102 and out["keepsPeak"]
