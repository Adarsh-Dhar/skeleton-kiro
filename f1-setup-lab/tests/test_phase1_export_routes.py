"""CSV export and HTML report routes (Requirements 7.1, 7.3, 7.4, 7.6, 7.7, 7.8)."""
from __future__ import annotations
import csv
import io
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from f1lab.api.app import create_app
@pytest.fixture
def api(tmp_path):
    app = create_app(str(tmp_path / "trialforge.db"))
    with TestClient(app) as c:
        svc = app.state.historical
        manifest = {"session_id": "s1", "provider": "openf1", "year": 2024, "event": "Test <GP>", "session_type": "R",
                    "session_date": "2024-03-01", "drivers": [{"code": "VER", "number": "1", "team": "RB"}], "channels": {}}
        laps = pd.DataFrame({"driver": ["1", "1"], "lap_number": [1, 2], "lap_time_s": [90.0, None]})
        weather = pd.DataFrame({"air_temp_c": [20.0, 22.0], "wind_direction_deg": [350.0, 10.0]})
        svc.repo.save_session("s1", manifest, laps, weather, {}, {})
        yield c
def _rows(text):
    return list(csv.reader(io.StringIO(text)))
def test_csv_matches_json_and_headers(api):
    r = api.get("/api/historical/sessions/s1/export.csv", params={"table": "laps"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/csv; charset=utf-8"
    assert "s1" in r.headers["content-disposition"] and "laps" in r.headers["content-disposition"]
    rows = _rows(r.text)
    js = api.get("/api/historical/sessions/s1/laps").json()
    assert len(rows) == 1 + len(js)
    col = rows[0].index("lap_time_s")
    assert [x[col] for x in rows[1:]] == ["90.0", ""]
    for t in ("results", "standings", "tyre_degradation"):
        out = api.get("/api/historical/sessions/s1/export.csv", params={"table": t})
        assert out.status_code == 200 and len(_rows(out.text)) == 1  # header only
    w = api.get("/api/historical/sessions/s1/export.csv", params={"table": "weather"})
    assert len(_rows(w.text)) == 3
def test_csv_errors(api):
    r = api.get("/api/historical/sessions/s1/export.csv", params={"table": "nope"})
    assert r.status_code == 422 and "mini_sectors" in r.text
    r = api.get("/api/historical/sessions/ghost/export.csv", params={"table": "laps"})
    assert r.status_code == 404
    assert api.get("/api/historical/sessions/ghost/report.html").status_code == 404
def test_report_is_escaped_and_complete(api):
    r = api.get("/api/historical/sessions/s1/report.html")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    body = r.text
    assert "Test &lt;GP&gt;" in body and "<script" not in body
    for heading in ("Summary", "Results", "Standings", "Tyre degradation", "Weather summary", "HISTORICAL F1 DATA"):
        assert heading in body
    assert "Wind direction" in body
