"""Tests for live telemetry exposure and the historical (FastF1) integration.

FastF1 itself is never hit here: the importer/normalizer are exercised against a fake session object,
and repository round-trips use real parquet files in tmp_path.
"""

from __future__ import annotations

import gzip
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from f1lab.integrations.game_udp.telemetry import TelemetryBuffer
from f1lab.integrations.game_udp.protocols import Header
from f1lab.integrations.historical import normalizer as N
from f1lab.integrations.historical.importer import _session_code, import_session, resolve_event
from f1lab.integrations.historical.repository import HistoricalRepository, session_id, slug
from f1lab.integrations.historical.service import HistoricalService


def _header(t=0.0):
    return Header(packet_format=2025, game_year=25, game_major=1, game_minor=0, packet_version=1, packet_id=6,
                  session_uid=7, session_time=t, frame_id=1, overall_frame_id=1000, player_car=0, secondary_car=255)


# ---------------------------------------------------------------- live telemetry API
def test_to_records_bounded_and_serializable():
    buf = TelemetryBuffer(max_samples=50)
    for i in range(10):
        buf.add(_header(i), {"speed_kph": 100 + i, "throttle": 0.5, "steer": 0.0, "brake": 0.0, "gear": 3, "rpm": 8000})
    recs = buf.to_records(4)
    assert len(recs) == 4 and recs[0]["session_time"] == 6.0 and recs[-1]["speed_kph"] == 109.0
    json.dumps(recs)  # must be JSON-safe


def test_live_telemetry_endpoint(client):
    r = client.get("/api/live/telemetry?scope=live&limit=100")
    assert r.status_code == 200
    body = r.json()
    assert body["samples"] == [] and body["count"] == 0 and body["scope"] == "live"
    assert client.get("/api/live/telemetry?scope=bogus").status_code == 422
    assert client.get("/api/live/telemetry?limit=0").status_code == 422
    assert client.get("/api/live/telemetry?limit=9999").status_code == 422


def test_stint_telemetry_series_roundtrip(client, tmp_path):
    """Persist + reload decoded stint telemetry via the evidence helpers."""
    from f1lab.integrations.game_udp.evidence import load_telemetry_series, save_telemetry_series

    buf = TelemetryBuffer()
    for i in range(5):
        buf.add(_header(float(i)), {"speed_kph": 50 + i, "throttle": 1, "steer": 0, "brake": 0, "gear": 4, "rpm": 9000})
    p = save_telemetry_series(tmp_path, "exp", "run-1", buf.to_records(100))
    assert p.exists() and p.suffixes[-2:] == [".jsonl", ".gz"]
    with gzip.open(p, "rt") as fh:
        assert len(fh.readlines()) == 5
    back = load_telemetry_series(tmp_path, "exp", "run-1")
    assert len(back) == 5 and back[0]["speed_kph"] == 50.0 and back[-1]["session_time"] == 4.0
    assert load_telemetry_series(tmp_path, "exp", "missing") is None


# ---------------------------------------------------------------- historical: repository
def test_session_id_safe_and_deterministic():
    sid = session_id(2024, "Italian Grand Prix", "Q")
    assert sid == "fastf1-2024-italian-grand-prix-q"
    assert "/" not in sid and ".." not in sid
    repo = HistoricalRepository("/tmp/x")
    with pytest.raises(ValueError):
        repo.manifest("../etc")


def test_repository_roundtrip(tmp_path):
    repo = HistoricalRepository(tmp_path / "hist")
    sid = session_id(2024, "Monza", "Q")
    manifest = {"session_id": sid, "provider": "fastf1", "year": 2024, "event": "Italian Grand Prix",
                "circuit": "Monza", "session": "Qualifying", "session_type": "Q", "drivers": [{"code": "VER"}],
                "channels": {}, "imported_at": "now", "import_status": "COMPLETED"}
    laps = pd.DataFrame([{"driver": "VER", "lap_number": 1, "lap_time_s": 80.1, "sector_1_s": 26.0,
                          "sector_2_s": 29.0, "sector_3_s": 25.1, "compound": "SOFT", "tyre_life": 2,
                          "is_valid": True, "stint": 1}])
    wx = pd.DataFrame([{"time_s": 0.0, "air_temp_c": 25.0, "track_temp_c": 40.0}])
    tel = {"VER": pd.DataFrame([{"lap_number": 1, "distance_m": 100.0, "speed_kph": 200.0, "throttle_pct": 80.0}])}
    pos = {"VER": pd.DataFrame([{"lap_number": 1, "x_m": 1.0, "y_m": 2.0, "speed_kph": 100.0}])}
    repo.save_session(sid, manifest, laps, wx, tel, pos)
    assert repo.exists(sid)
    assert repo.manifest(sid)["event"] == "Italian Grand Prix"
    assert repo.telemetry(sid, "ver")["speed_kph"].iloc[0] == 200.0  # case-insensitive driver lookup
    assert repo.laps(sid).iloc[0]["lap_time_s"] == 80.1
    assert repo.weather(sid).iloc[0]["air_temp_c"] == 25.0
    assert repo.position(sid, "VER").iloc[0]["x_m"] == 1.0
    assert [s["session_id"] for s in repo.list_sessions()] == [sid]
    # parquet files exist on disk
    assert (repo.sessions_dir / sid / "laps.parquet").exists()
    assert (repo.sessions_dir / sid / "telemetry" / "ver.parquet").exists()


# ---------------------------------------------------------------- historical: normalizer
def _fake_session():
    car = pd.DataFrame({"Time": [timedelta(seconds=i) for i in range(4)],
                        "SessionTime": [timedelta(seconds=i) for i in range(4)],
                        "Speed": [100, 150, 200, 250], "Throttle": [50, 80, 100, 100],
                        "Brake": [0, 10, 0, 0], "Gear": [3, 4, 5, 6], "RPM": [8000, 9000, 10000, 11000],
                        "DRS": [0, 0, 1, 1], "Steering": [0.1, 0.0, -0.1, 0.0]})
    pos = pd.DataFrame({"X": [0.0, 1.0, 2.0], "Y": [0.0, 1.0, 0.0], "Z": [0.0, 0.0, 0.0],
                        "Speed": [100, 150, 200], "SessionTime": [timedelta(seconds=i) for i in range(3)]})
    laps_rows = []
    for no in (1, 2):
        for drv in ("VER", "LEC"):
            laps_rows.append({"LapNumber": no, "Driver": drv, "LapTime": timedelta(seconds=80 + no),
                              "Sector1Time": timedelta(seconds=26), "Sector2Time": timedelta(seconds=29),
                              "Sector3Time": timedelta(seconds=25), "Compound": "SOFT", "TyreLife": no,
                              "Stint": 1, "IsPersonalBest": no == 1 and drv == "VER", "DriverNumber": 1, "Team": "Red Bull"})
    laps = pd.DataFrame(laps_rows)

    class Lap:
        def __init__(self, row):
            self._row = row
        def get_car_data(self):
            return car
        def get_pos_data(self):
            return pos

    class LapRow(pd.Series):
        """Mimics FastF1's Lap row: a Series with get_car_data()/get_pos_data() attached."""
        @property
        def _constructor(self):
            return pd.Series

        @property
        def _constructor_expanddim(self):
            return pd.DataFrame

        def get_car_data(self):
            return car

        def get_pos_data(self):
            return pos

    class LapsView:
        """Minimal stand-in for session.laps: supports len(), column access, boolean masking, iterrows()."""
        def __init__(self, df):
            self.df = df
        def __len__(self):
            return len(self.df)
        def __getitem__(self, key):
            res = self.df[key]
            return LapsView(res) if isinstance(res, pd.DataFrame) else res
        @property
        def columns(self):
            return self.df.columns
        def iterrows(self):
            for i, r in self.df.iterrows():
                yield i, LapRow(r)

    drivers = {"VER": SimpleNamespace(first_name="Max", last_name="Verstappen", team_name="Red Bull Racing"),
               "LEC": SimpleNamespace(first_name="Charles", last_name="Leclerc", team_name="Ferrari")}

    def get_driver(code):
        return drivers[code]

    event = {"EventName": "Italian Grand Prix", "OfficialEventName": "Italian Grand Prix", "Location": "Monza",
             "Round": 16, "SessionName": "Qualifying", "SessionDate": "2024-09-01", "CircuitShortName": "Monza"}
    return SimpleNamespace(
        drivers=["VER", "LEC"], get_driver=get_driver, laps=LapsView(laps), event=event,
        weather_data=pd.DataFrame([{"Time": timedelta(seconds=0), "AirTemp": 25.0, "TrackTemp": 40.0,
                                     "Humidity": 55.0, "WindSpeed": 2.0, "Rainfall": 0.0, "Pressure": 1013.0}]),
        circuit_info=None,
    )


def test_normalize_laps_timedelta_and_flags():
    sess = _fake_session()
    df = N.normalize_laps(sess)
    assert len(df) == 4
    ver = df[(df.driver == "VER") & (df.lap_number == 1)].iloc[0]
    assert ver.lap_time_s == pytest.approx(81.0)
    assert ver.sector_1_s == pytest.approx(26.0) and ver.compound == "SOFT" and ver.tyre_life == 1
    assert ver.is_personal_best == True and ver.is_valid is None  # no validity column -> preserved as missing


def test_normalize_weather():
    wx = N.normalize_weather(_fake_session())
    assert len(wx) == 1 and wx.iloc[0].air_temp_c == 25.0 and wx.iloc[0].track_temp_c == 40.0


def test_driver_telemetry_and_position():
    sess = _fake_session()
    tel = N.driver_telemetry(sess, "VER")
    assert set(tel.lap_number.unique()) == {1, 2}
    assert tel.columns.tolist()[:2] == ["lap_number", "distance_m"] or "distance_m" in tel.columns
    assert tel.speed_kph.max() == 250.0 and tel.throttle_pct.max() == 100.0
    pos = N.driver_position(sess, "VER")
    assert len(pos) > 0 and "x_m" in pos.columns and "y_m" in pos.columns


def test_driver_list():
    ds = N.driver_list(_fake_session())
    assert {d["code"] for d in ds} == {"VER", "LEC"}
    assert ds[0]["name"] == "Max Verstappen" and ds[0]["team"] == "Red Bull Racing"


# ---------------------------------------------------------------- historical: importer + service
def test_session_code_validation():
    assert _session_code("q") == "Q" and _session_code("Qualifying") == "Q" and _session_code("R") == "R"
    with pytest.raises(ValueError):
        _session_code("WARMUP")


def test_resolve_event():
    sched = pd.DataFrame([{"EventName": "Italian Grand Prix", "Location": "Monza", "SessionType": "Q",
                           "SessionName": "Qualifying", "SessionDate": "2024-09-01"},
                          {"EventName": "Singapore Grand Prix", "Location": "Singapore", "SessionType": "Q",
                           "SessionName": "Qualifying", "SessionDate": "2024-10-20"}])
    assert resolve_event(sched, "Monza") == "Italian Grand Prix"
    assert resolve_event(sched, "italian grand prix") == "Italian Grand Prix"
    with pytest.raises(ValueError):
        resolve_event(sched, "Atlantis")


class _FakeClient:
    cache_dir = Path("/tmp/fake-cache")

    def get_schedule(self, year):
        return pd.DataFrame([{"EventName": "Italian Grand Prix", "Location": "Monza", "SessionType": "Q",
                              "SessionName": "Qualifying", "SessionDate": "2024-09-01"}])

    def load_session(self, year, event, session_type, **kw):
        return _fake_session()


def test_import_session_end_to_end(tmp_path):
    repo = HistoricalRepository(tmp_path / "hist")
    manifest = import_session(_FakeClient(), repo, {"year": 2024, "event": "Monza", "session": "Q",
                                                   "drivers": ["VER"], "include_weather": True,
                                                   "include_telemetry": True, "include_position": True})
    assert manifest["session_id"] == "fastf1-2024-italian-grand-prix-q"
    assert manifest["import_status"] == "COMPLETED"
    assert manifest["channels"]["telemetry"]["VER"] is True
    assert repo.exists(manifest["session_id"])
    # stored data readable through the repository
    assert len(repo.laps(manifest["session_id"]).query("driver == 'VER'").lap_number) == 2
    assert len(repo.telemetry(manifest["session_id"], "VER")) == 8  # 4 samples x 2 laps


def test_import_bad_driver_rejected(tmp_path):
    repo = HistoricalRepository(tmp_path / "hist")
    with pytest.raises(ValueError, match="not in this session"):
        import_session(_FakeClient(), repo, {"year": 2024, "event": "Monza", "session": "Q",
                                             "drivers": ["XYZ"]})


def test_service_queries(tmp_path):
    repo_root = tmp_path / "data"
    svc = HistoricalService(repo_root)
    repo = svc.repo
    sid = session_id(2024, "Monza", "Q")
    manifest = {"session_id": sid, "provider": "fastf1", "year": 2024, "event": "Italian Grand Prix",
                "circuit": "Monza", "session": "Qualifying", "session_type": "Q", "session_date": "2024-09-01",
                "drivers": [{"code": "VER", "name": "Max Verstappen", "team": "Red Bull"},
                            {"code": "LEC", "name": "Charles Leclerc", "team": "Ferrari"}],
                "channels": {}, "corners": [{"number": 1, "name": "T1", "distance_m": 100.0}],
                "imported_at": "now", "import_status": "COMPLETED", "attribution": "x"}
    tel = {"VER": pd.DataFrame([{"lap_number": 1, "distance_m": float(i * 100), "session_time_s": float(i),
                                 "speed_kph": 100 + i * 10, "throttle_pct": 50.0, "brake_pct": 0.0,
                                 "rpm": 9000.0, "gear": 4.0, "drs": 0.0} for i in range(10)]),
           "LEC": pd.DataFrame([{"lap_number": 1, "distance_m": float(i * 100), "session_time_s": float(i),
                                 "speed_kph": 90 + i * 10, "throttle_pct": 40.0, "brake_pct": 5.0,
                                 "rpm": 8500.0, "gear": 4.0, "drs": 0.0} for i in range(10)])}
    laps = pd.DataFrame([{"driver": d, "lap_number": 1, "lap_time_s": 80.0 + (0 if d == "VER" else 1),
                          "sector_1_s": 26.0, "sector_2_s": 29.0, "sector_3_s": 25.0, "compound": "SOFT",
                          "tyre_life": 2, "is_valid": True, "stint": 1} for d in ("VER", "LEC")])
    repo.save_session(sid, manifest, laps, pd.DataFrame(), tel, {})

    assert [s["session_id"] for s in svc.list_sessions()] == [sid]
    assert svc.get_session(sid)["event"] == "Italian Grand Prix"
    assert len(svc.drivers(sid)) == 2
    assert len(svc.laps(sid, "ver")) == 1  # case-insensitive
    t = svc.telemetry(sid, "VER", lap=1, limit=5)
    assert t["count"] == 10 and len(t["samples"]) == 5
    tm = svc.track_map(sid, "VER", 1)
    assert tm["corners"][0]["number"] == 1
    with pytest.raises(ValueError):
        svc.telemetry(sid, "ZZZ")
    # compare: VER is faster early? both same time base; delta should be finite
    from f1lab.integrations.historical.schemas import HistoricalCompareRequest
    cmp = svc.compare(sid, HistoricalCompareRequest(driver_a="VER", lap_a=1, driver_b="LEC", lap_b=1, points=20))
    assert len(cmp["delta"]["delta_s"]) == 20
    assert "interpolated" in cmp["delta"]["note"]


def test_service_import_job_lifecycle(tmp_path, monkeypatch):
    import asyncio

    svc = HistoricalService(tmp_path / "data")
    calls = []

    def fake_import(client, repo, payload, progress):
        calls.append(payload["event"])
        progress("DOWNLOADING", "fake")
        progress("COMPLETED", "done")
        return {"session_id": "fastf1-2024-x-q"}

    monkeypatch.setattr("f1lab.integrations.historical.service.import_session", fake_import)
    from f1lab.integrations.historical.schemas import HistoricalImportRequest
    req = HistoricalImportRequest(year=2024, event="Monza", session="Q", drivers=["VER"])

    async def go():
        r = await svc.start_import(req)
        for _ in range(50):
            j = svc.import_status(r["import_id"])
            if j["state"] == "COMPLETED":
                return j
            await asyncio.sleep(0.02)
        return svc.import_status(r["import_id"])

    job = asyncio.run(go())
    assert job["state"] == "COMPLETED" and job["session_id"] == "fastf1-2024-x-q"
    assert calls == ["Monza"]
    from f1lab.services.lab import NotFound
    with pytest.raises(NotFound):
        svc.import_status("imp-nope")
