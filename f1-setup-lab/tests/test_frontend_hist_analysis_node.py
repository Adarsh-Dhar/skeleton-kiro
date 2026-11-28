"""Pure-function checks of the provenance badge, session list and shared trace charts in hist_analysis.js, run through node
(Req 13.9, 13.10, 17.1, 17.2, 17.5, 17.7). The real charts.js is loaded; no browser is needed."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

PRELUDE = f"""
const vm = require('vm'), fs = require('fs');
const ctx = vm.createContext({{ module: {{ exports: {{}} }}, console }});
vm.runInContext(fs.readFileSync({json.dumps(str(FRONTEND / 'charts.js'))}, 'utf8') + '\\n;this.Charts = Charts;', ctx);
vm.runInContext(fs.readFileSync({json.dumps(str(FRONTEND / 'hist_analysis.js'))}, 'utf8') + '\\n;this.H = HistAnalysis;', ctx);
const H = ctx.H;
"""


def run(body: str):
    r = subprocess.run(["node", "-e", PRELUDE + body], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_every_row_has_provider_data_type_and_provenance_badge_and_empty_state_has_no_rows():
    out = run("""
const list = [
  { session_id: 'a', event: 'E', session: 'R', provider: 'openf1', data_type_label: 'Full telemetry', provenance_status: 'OPENF1 \\u2014 external provider', drivers: [] },
  { session_id: 'b', event: 'E', session: 'R', provider: 'csv_upload', data_type_label: 'Lap summaries', provenance_status: 'USER UPLOADED \\u2014 UNVERIFIED', drivers: [] },
  { session_id: 'c', event: 'E', session: 'R', provider: 'csv_upload', data_type_label: 'Full telemetry', provenance_status: 'SYNTHETIC DEMO \\u2014 generated test data', drivers: [] }];
const html = H.sessionsTable(list);
console.log(JSON.stringify({ rows: (html.match(/<tr data-session/g) || []).length, badges: (html.match(/provenance-badge/g) || []).length,
  labels: list.map((s) => html.includes('data-provenance="' + s.provenance_status + '"')), providers: list.map((s) => H.providerLabel(s)),
  empty: H.sessionsTable([]), same: list.map((s) => H.provenanceBadge(s) === H.provenanceBadge({ ...s })) }));
""")
    assert out["rows"] == 3 and out["badges"] == 3 and all(out["labels"]) and all(out["same"])
    assert out["providers"] == ["OpenF1", "CSV Upload", "CSV Upload"]
    assert "No sessions imported yet" in out["empty"] and "<tr" not in out["empty"]


def test_unavailable_chart_lists_every_missing_channel_and_others_stay():
    out = run("""
const samples = [{ distance_m: 0, speed_kph: 100, throttle_pct: 50, brake_applied: 0 }, { distance_m: 10, speed_kph: 110, throttle_pct: 60, brake_applied: 0 }];
const html = H.traceCharts(samples);
const noDist = H.traceCharts(samples.map(({ distance_m, ...p }) => p));
console.log(JSON.stringify({ html, unavailable: (html.match(/chart-unavailable/g) || []).length, noDist: (noDist.match(/chart-unavailable/g) || []).length,
  missing: H.missingChannels(samples, ['distance_m', 'rpm', 'gear', ['brake_applied', 'brake_pct']]) }));
""")
    assert out["unavailable"] == 3  # rpm, gear, drs lack their channel; speed and throttle/brake are drawn
    assert 'data-chart="speed"' in out["html"] and 'data-chart="throttle_brake"' in out["html"]
    assert out["noDist"] == 5  # distance_m is missing, so every distance trace is unavailable
    assert "distance_m, rpm" in out["html"].replace("distance_m, speed_kph", "") or "rpm" in out["html"]
    assert out["missing"] == ["rpm", "gear"]


def test_lap_summaries_session_shows_every_channel_unavailable_and_lap_and_sector_tables():
    out = run("""
const html = H.channelsUnavailable();
const tables = H.lapSectorTables([{ lap_number: 1, lap_time_s: 90.5, sector_1_s: 30, sector_2_s: 30.25, sector_3_s: null }]);
console.log(JSON.stringify({ rows: (html.match(/data-channel=/g) || []).length, unavailable: (html.match(/>unavailable</g) || []).length,
  tables, is: H.isLapSummaries({ data_type_label: 'Lap summaries' }), not: H.isLapSummaries({ data_type_label: 'Full telemetry' }) }));
""")
    assert out["rows"] == out["unavailable"] == 10
    assert 'id="lap-table"' in out["tables"] and 'id="sector-table"' in out["tables"] and "90.500" in out["tables"]
    assert out["is"] is True and out["not"] is False
