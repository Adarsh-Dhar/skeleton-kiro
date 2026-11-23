"""Phase 1 historical routes: error table, empty data and table access
(Requirements 4.4, 5.4, 6.6, 7.6, 7.7, 8.1, 9.1, 9.3). Fixtures only, no network."""
from __future__ import annotations
import csv
import io
import json
import re
import sys
import time
from pathlib import Path
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from f1lab.api.app import create_app
from f1lab.integrations.historical.openf1_client import OpenF1Client, RateLimiter
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import gen_historical_manifest as gen  # noqa: E402
PRE = "/api/historical"
FIXTURES = sorted((Path(__file__).parent / "fixtures" / "openf1").glob("*.json"))
def _import(http, year, event_name, code, driver):
    iid = http.post(f"{PRE}/import", json={"year": year, "event": event_name, "session": code, "drivers": [driver]}).json()["import_id"]
    for _ in range(1200):
        s = http.get(f"{PRE}/imports/{iid}").json()
        if s["state"] in ("COMPLETED", "FAILED", "CANCELLED"):
            break
        time.sleep(0.05)
    assert s["state"] == "COMPLETED", s
    return s["session_id"]
@pytest.fixture(scope="module")
def imported(tmp_path_factory):
    """One app with all five fixture sessions imported; yields (http, [(session_id, driver)])."""
    tmp = tmp_path_factory.mktemp("phase1-routes")
    fetchers = {}
    for f in FIXTURES:
        d = json.loads(f.read_text())
        fetchers[f.stem] = d
    # one app per fixture is needed because the fake fetch serves one fixture; use a dispatching client per fixture
    apps, out = [], []
    for f in FIXTURES:
        d = fetchers[f.stem]
        sess, meeting = d["sessions"][0], d["meetings"][0]
        num = str(d["_meta"]["driver_number"])
        drv = next(x["name_acronym"] for x in d["drivers"] if str(x["driver_number"]) == num)
        p = tmp / f.stem
        client = OpenF1Client(p / "cache", fetch=gen.fixture_fetch(d), limiter=RateLimiter(per_second=10_000, per_minute=10_000_000), sleep=lambda s: None)
        app = create_app(str(p / "trialforge.db"), frontend_dir=str(p / "none"), openf1_client_factory=lambda c=client: c)
        http = TestClient(app)
        http.__enter__()
        apps.append(http)
        out.append((http, _import(http, int(sess["year"]), meeting["meeting_name"], gen.CODES[sess["session_name"]], drv), drv))
    yield out
    for h in apps:
        h.__exit__(None, None, None)
@pytest.fixture
def synth(tmp_path):
    """Empty app plus a helper to store hand-built sessions."""
    app = create_app(str(tmp_path / "trialforge.db"))
    with TestClient(app) as c:
        repo = app.state.historical.repo
        def store(sid, laps=None, telemetry=None, datasets=None, drivers=None):
            manifest = {"session_id": sid, "provider": "openf1", "year": 2024, "event": sid, "session_type": "R",
                        "session_date": "2024-03-01", "drivers": drivers or [{"code": "VER", "number": "1", "team": "RB"}], "channels": {}}
            repo.save_session(sid, manifest, laps if laps is not None else pd.DataFrame(), pd.DataFrame(), telemetry or {}, {}, datasets)
        c.store = store
        c.app_ = app
        yield c
# ----------------------------------------------------------- 404 on every new route, on each fixture
def test_unknown_session_404_on_every_new_route(imported):
    for http, _sid, drv in imported:
        for path, params in (("mini-sectors", {"driver": drv}), ("tyre-degradation", {}), ("report.html", {}),
                             ("export.csv", {"table": "laps"})):
            r = http.get(f"{PRE}/sessions/ghost/{path}", params=params)
            assert r.status_code == 404 and "ghost" in r.text, (path, r.status_code)
        r = http.post(f"{PRE}/compare-sessions", json={"sessions": ["ghost", "ghost2"], "drivers": [drv], "metric": "points"})
        assert r.status_code == 404 and "ghost" in r.text
# ----------------------------------------------------------- 422 cases on each fixture
def test_validation_422(imported):
    for http, sid, drv in imported:
        base = f"{PRE}/sessions/{sid}"
        for count in (9, 51, 0, -1):
            r = http.get(f"{base}/mini-sectors", params={"driver": drv, "count": count})
            assert r.status_code == 422 and "count" in r.text, count
        r = http.get(f"{base}/mini-sectors", params={"driver": "ZZZ"})
        assert r.status_code == 422 and "ZZZ" in r.text
        r = http.get(f"{base}/mini-sectors", params={"driver": drv, "ref_driver": "ZZZ"})
        assert r.status_code == 422
        r = http.get(f"{base}/export.csv", params={"table": "nope"})
        assert r.status_code == 422 and "mini_sectors" in r.text
        r = http.get(f"{base}/export.csv", params={"table": "mini_sectors"})
        assert r.status_code == 422 and "driver" in r.text
        bad = [({"sessions": [sid], "drivers": [drv], "metric": "points"}, "sessions"),
               ({"sessions": [sid] * 25, "drivers": [drv], "metric": "points"}, "sessions"),
               ({"sessions": [sid, sid], "drivers": [], "metric": "points"}, "drivers"),
               ({"sessions": [sid, sid], "drivers": [drv] * 7, "metric": "points"}, "drivers"),
               ({"sessions": [sid, sid], "drivers": [drv], "metric": "speed"}, "metric")]
        for body, field in bad:
            r = http.post(f"{PRE}/compare-sessions", json=body)
            assert r.status_code == 422 and field in r.text, body
# ----------------------------------------------------------- fixtures: happy paths and empty championship data
def test_fixture_sessions_serve_new_routes(imported):
    for http, sid, drv in imported:
        base = f"{PRE}/sessions/{sid}"
        t = http.get(f"{base}/tyre-degradation")
        assert t.status_code == 200 and t.json()["caveat"]
        assert http.get(f"{base}/report.html").status_code == 200
        r = http.get(f"{base}/export.csv", params={"table": "standings"})
        assert r.status_code == 200
        rows = list(csv.reader(io.StringIO(r.text)))
        if len(rows) == 1:  # no championship dataset stored: header only, and the report says so
            assert "unavailable" in http.get(f"{base}/report.html").text
# ----------------------------------------------------------- 409 mini-sectors
def _tel(dist, times):
    return pd.DataFrame({"lap_number": [1] * len(dist), "distance_m": dist, "session_time_s": times})
def test_mini_sectors_409_decreasing_distance(synth):
    n = 40
    laps = pd.DataFrame({"driver": ["1"], "lap_number": [1], "lap_time_s": [80.0]})
    synth.store("bad", laps, {"VER": _tel([float(n - i) for i in range(n)], [float(i) for i in range(n)])})
    r = synth.get(f"{PRE}/sessions/bad/mini-sectors", params={"driver": "VER", "lap": 1})
    assert r.status_code == 409 and "distance_m" in r.text
    r = synth.get(f"{PRE}/sessions/bad/export.csv", params={"table": "mini_sectors", "driver": "VER", "lap": 1})
    assert r.status_code == 409
def test_mini_sectors_409_no_telemetry_and_ok_case(synth):
    laps = pd.DataFrame({"driver": ["1"], "lap_number": [1], "lap_time_s": [80.0]})
    synth.store("none", laps)
    r = synth.get(f"{PRE}/sessions/none/mini-sectors", params={"driver": "VER"})
    assert r.status_code == 409 and "telemetry" in r.text
    n = 40
    synth.store("good", laps, {"VER": _tel([100.0 * i for i in range(n)], [2.0 * i for i in range(n)])})
    r = synth.get(f"{PRE}/sessions/good/mini-sectors", params={"driver": "VER", "count": 10})
    assert r.status_code == 200 and len(r.json()["slices"]) == 10
    assert abs(sum(s["time_s"] for s in r.json()["slices"]) - 78.0) < 1e-9
# ----------------------------------------------------------- tyre degradation without stints
def test_tyre_degradation_empty_with_coverage_note(synth):
    laps = pd.DataFrame({"driver": ["1", "1"], "lap_number": [1, 2], "lap_time_s": [90.0, 91.0]})
    synth.store("nostint", laps)
    r = synth.get(f"{PRE}/sessions/nostint/tyre-degradation")
    assert r.status_code == 200
    body = r.json()
    assert body["stints"] == [] and body["coverage_note"] and body["caveat"]
    synth.store("nolaps")
    r = synth.get(f"{PRE}/sessions/nolaps/tyre-degradation")
    assert r.status_code == 200 and r.json()["stints"] == []
# ----------------------------------------------------------- standings without championship data
def test_missing_championship_header_only_and_report_unavailable(synth):
    synth.store("nochamp", pd.DataFrame({"driver": ["1"], "lap_number": [1], "lap_time_s": [90.0]}))
    r = synth.get(f"{PRE}/sessions/nochamp/export.csv", params={"table": "standings"})
    assert r.status_code == 200
    rows = list(csv.reader(io.StringIO(r.text)))
    assert len(rows) == 1 and rows[0][:2] == ["kind", "position"]
    rep = synth.get(f"{PRE}/sessions/nochamp/report.html")
    assert rep.status_code == 200 and "Standings" in rep.text and "unavailable" in rep.text
    # with data stored, the same table fills in
    synth.store("champ", datasets={"drivers_championship": ([{"driver": "VER", "points_start": 0, "points_current": 25,
                                                              "position_start": 1, "position_current": 1}], None)})
    rows = list(csv.reader(io.StringIO(synth.get(f"{PRE}/sessions/champ/export.csv", params={"table": "standings"}).text)))
    assert len(rows) == 2 and "VER" in rows[1]
# ----------------------------------------------------------- driver absent from a session
def test_driver_absent_gives_null_cell_with_reason(synth):
    laps = pd.DataFrame({"driver": ["1", "1"], "lap_number": [1, 2], "lap_time_s": [90.0, 88.0]})
    synth.store("a", laps)
    synth.store("b", laps)
    r = synth.post(f"{PRE}/compare-sessions", json={"sessions": ["a", "b"], "drivers": ["VER", "HAM"], "metric": "best_lap"})
    assert r.status_code == 200
    cells = {(c["session_id"], c["driver"]): c for c in r.json()["cells"]}
    assert len(cells) == 4
    for sid in ("a", "b"):
        assert cells[(sid, "VER")]["value"] == 88.0 and cells[(sid, "VER")]["reason"] is None
        assert cells[(sid, "HAM")]["value"] is None and cells[(sid, "HAM")]["reason"]
# ----------------------------------------------------------- read-only table access
def test_new_routes_touch_only_historical_tables(imported):
    http, sid, drv = imported[0]
    engine = http.app.state.historical.repo.engine
    seen: list[str] = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)
    event.listen(engine, "before_cursor_execute", capture)
    try:
        base = f"{PRE}/sessions/{sid}"
        http.get(f"{base}/tyre-degradation")
        http.get(f"{base}/mini-sectors", params={"driver": drv})
        http.get(f"{base}/report.html")
        for t in ("laps", "results", "standings", "weather", "tyre_degradation"):
            http.get(f"{base}/export.csv", params={"table": t})
        http.post(f"{PRE}/compare-sessions", json={"sessions": [sid, sid], "drivers": [drv], "metric": "points"})
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert seen
    tables = set()
    for stmt in seen:
        assert not re.match(r"\s*(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE)", stmt, re.I), stmt
        tables |= set(re.findall(r'(?:FROM|JOIN)\s+"?([A-Za-z_][A-Za-z0-9_]*)"?', stmt, re.I))
    assert tables, seen[:3]
    assert all(t.startswith("f1_hist_") or t == "f1_import_job" for t in tables), tables
