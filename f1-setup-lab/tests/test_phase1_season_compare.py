"""Season comparison route and OpenF1 laps without validity (Requirements 4.1 to 4.7)."""
from __future__ import annotations

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from f1lab.api.app import create_app


@pytest.fixture
def api(tmp_path):
    with TestClient(create_app(str(tmp_path / "trialforge.db"))) as c:
        yield c


def test_validation_and_404_and_values(tmp_path):
    from db import create_db_engine, migrate
    from f1lab.errors import NotFound
    from f1lab.integrations.historical.schemas import SeasonCompareRequest
    from f1lab.integrations.historical.service import HistoricalService

    engine = create_db_engine(tmp_path / "t.db")
    migrate(engine)
    svc = HistoricalService(engine, tmp_path / "cache")
    for sid, date, valid in (("a", "2024-03-01", None), ("b", "2024-04-01", True)):
        manifest = {"session_id": sid, "provider": "openf1", "source_type": "external_provider", "verification": "verified", "year": 2024, "event": sid.upper(), "session_type": "R",
                    "session_date": date, "drivers": [{"code": "VER", "number": "1"}], "channels": {}}
        laps = pd.DataFrame({"driver": ["1", "1", "1"], "lap_number": [1, 2, 3], "lap_time_s": [90.0, 80.0, 85.0],
                             "is_valid": [valid] * 3, "pits": [True, False, False]})
        svc.repo.save_session(sid, manifest, laps, pd.DataFrame(), {}, {})

    req = SeasonCompareRequest(sessions=["b", "a"], drivers=["ver", "HAM"], metric="best_lap")
    out = svc.compare_sessions(req)
    assert [s["id"] for s in out["sessions"]] == ["a", "b"]
    cell = {(c["session_id"], c["driver"]): c for c in out["cells"]}
    assert cell[("a", "VER")]["value"] == 80.0  # missing validity is not an exclusion; pit lap still is
    assert cell[("b", "VER")]["value"] == 80.0
    assert cell[("a", "HAM")]["value"] is None and cell[("a", "HAM")]["reason"]

    med = svc.compare_sessions(SeasonCompareRequest(sessions=["a", "b"], drivers=["VER"], metric="median_lap"))
    assert med["cells"][0]["value"] == 82.5

    grid = svc.compare_sessions(SeasonCompareRequest(sessions=["a", "b"], drivers=["VER"], metric="grid_position"))
    assert grid["cells"][0]["value"] is None and "not stored" in grid["cells"][0]["reason"]

    with pytest.raises(NotFound) as e:
        svc.compare_sessions(SeasonCompareRequest(sessions=["a", "nope"], drivers=["VER"], metric="points"))
    assert "nope" in str(e.value)


def test_route_errors(api):
    r = api.post("/api/historical/compare-sessions", json={"sessions": ["a"], "drivers": ["VER"], "metric": "points"})
    assert r.status_code == 422 and "sessions" in r.text
    r = api.post("/api/historical/compare-sessions", json={"sessions": ["a", "b"], "drivers": ["VER"], "metric": "speed"})
    assert r.status_code == 422 and "metric" in r.text
    r = api.post("/api/historical/compare-sessions", json={"sessions": ["a", "b"], "drivers": ["V"] * 7, "metric": "points"})
    assert r.status_code == 422 and "drivers" in r.text
    r = api.post("/api/historical/compare-sessions", json={"sessions": ["ghost", "b"], "drivers": ["VER"], "metric": "points"})
    assert r.status_code == 404 and "ghost" in r.text
