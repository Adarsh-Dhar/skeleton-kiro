"""Task 11 (F1-K4): regression tests that keep the historical-data fixes from being broken again.

Fixtures are small synthetic FastF1 look-alikes; no network access.
"""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pandas as pd
import pytest
from test_historical import _fake_session, _FakeClient

from db import create_db_engine, migrate
from f1lab.integrations.historical import normalizer as N
from f1lab.integrations.historical.importer import import_session
from f1lab.integrations.historical.repository import HistoricalRepository, session_id
from f1lab.integrations.historical.schemas import HistoricalCompareRequest
from f1lab.integrations.historical.service import HistoricalService


@pytest.fixture
def engine(tmp_path):
    e = create_db_engine(tmp_path / "trialforge.db")
    migrate(e)
    return e


# ------------------------------------------------------------------ lap validity (steward deletions vs timing accuracy)
def test_deleted_and_accurate_flags_stay_independent():
    df = N.normalize_laps(_fake_session())
    lec2 = df[(df.driver == "LEC") & (df.lap_number == 2)].iloc[0]
    ver2 = df[(df.driver == "VER") & (df.lap_number == 2)].iloc[0]
    assert lec2.is_deleted == True and lec2.is_valid == False and lec2.deleted_reason == "Time deleted"  # noqa: E712
    assert ver2.is_accurate == False and ver2.is_valid == True  # noqa: E712  inaccurate timing is NOT an invalid lap
    assert str(df.is_deleted.dtype) == "boolean" and str(df.is_accurate.dtype) == "boolean"


def test_explicit_isvalid_is_kept_when_no_deletion_info():
    s = _fake_session()
    s.laps.df = s.laps.df.drop(columns=["Deleted", "DeletedReason"]).assign(IsValid=[True, False, True, None])
    df = N.normalize_laps(s)
    assert df.is_valid.tolist()[:3] == [True, False, True] and pd.isna(df.is_valid.iloc[3])
    assert df.is_deleted.isna().all()


def test_invalid_and_deleted_laps_survive_storage(engine, tmp_path):
    repo = HistoricalRepository(engine, tmp_path / "c")
    m = import_session(_FakeClient(), repo, {"year": 2024, "event": "Monza", "session": "Q", "drivers": ["LEC"]})
    laps = repo.laps(m["session_id"])
    lec2 = laps[(laps.driver == "LEC") & (laps.lap_number == 2)].iloc[0]
    assert lec2.is_deleted == True and lec2.is_valid == False  # noqa: E712


# ------------------------------------------------------------------ brake channel
def test_boolean_brake_is_on_off_not_a_percentage():
    applied, pct = N._brake_channels(pd.Series([False, True, None], dtype="boolean"))
    assert applied == [0.0, 100.0, None] and pct is None
    applied, pct = N._brake_channels(pd.Series([0.0, 35.5, 100.0]))
    assert applied is None and pct == [0.0, 35.5, 100.0]  # a genuine percentage stays a percentage
    assert N._brake_channels(pd.Series([None, None])) == (None, None)


# ------------------------------------------------------------------ missing telemetry values
def test_missing_telemetry_values_are_not_fabricated():
    car = pd.DataFrame({"SessionTime": [timedelta(seconds=i) for i in range(4)], "Distance": [0.0, 10.0, 20.0, 30.0],
                        "Speed": [100, None, 120, None], "Throttle": [None, None, None, None], "RPM": [9000, 9100, None, 9300]})
    out = N._car_frame(car)
    # a channel without data stays null (missing), never zero-filled; the source has no Gear column at all
    assert out["throttle_pct"].isna().all() and out["gear"].isna().all()
    assert not (out.fillna(-1) == 0).drop(columns=["distance_m", "session_time_s"]).any().any()
    assert out.speed_kph.isna().sum() == 2 and len(out) == 4
    empty = N._car_frame(pd.DataFrame({"SessionTime": [timedelta(seconds=1)], "Speed": [None]}))
    assert empty.empty or empty.drop(columns=[c for c in ("session_time_s",) if c in empty.columns]).isna().all().all()


# ------------------------------------------------------------------ circuit corners
def test_corners_from_get_circuit_info():
    corners = pd.DataFrame({"X": [1.0, 2.0], "Y": [3.0, 4.0], "Number": [1, 2], "Letter": ["", "a"], "Angle": [0, 0], "Distance": [100.0, 250.0]})
    s = _fake_session()
    s.get_circuit_info = lambda: SimpleNamespace(corners=corners)
    out = N.circuit_corners(s)
    assert [c["number"] for c in out] == [1.0, 2.0] and out[1]["letter"] == "a" and out[1]["distance_m"] == 250.0 and out[0]["x_m"] == 1.0


def test_missing_circuit_coordinates_and_failing_circuit_info():
    s = _fake_session()
    s.get_circuit_info = lambda: SimpleNamespace(corners=pd.DataFrame({"Number": [1], "Distance": [50.0]}))
    out = N.circuit_corners(s)
    assert out[0]["x_m"] is None and out[0]["y_m"] is None and out[0]["distance_m"] == 50.0

    def boom():
        raise RuntimeError("circuit info unavailable")
    s.get_circuit_info = boom
    assert N.circuit_corners(s) == []


def test_position_without_xy_is_dropped():
    assert N._pos_frame(pd.DataFrame({"Speed": [100, 110]})).empty
    out = N._pos_frame(pd.DataFrame({"X": [1.0, None], "Y": [2.0, 3.0]}))
    assert len(out) == 1  # rows without both coordinates are not plotted


# ------------------------------------------------------------------ drivers
def test_driver_lookup_by_code_and_number(engine, tmp_path):
    svc = HistoricalService(engine, tmp_path / "c")
    sid = session_id(2024, "Monza", "Q")
    manifest = {"session_id": sid, "event": "Italian Grand Prix", "drivers": [{"code": "VER", "number": "1"}, {"code": "LEC", "number": "16"}], "channels": {}}
    laps = pd.DataFrame([{"driver": "1", "lap_number": 1, "lap_time_s": 80.0}, {"driver": "LEC", "lap_number": 1, "lap_time_s": 81.0}])
    tel = {"VER": pd.DataFrame({"lap_number": [1, 1], "distance_m": [0.0, 10.0], "session_time_s": [0.0, 1.0], "speed_kph": [100.0, 110.0]})}
    svc.repo.save_session(sid, manifest, laps, pd.DataFrame(), tel, {})
    assert len(svc.laps(sid, "1")) == 1 and len(svc.laps(sid, "ver")) == 1  # laps keyed by number are found via the code
    assert svc.telemetry(sid, "1")["driver"] == "VER" and svc.telemetry(sid, "16")["count"] == 0
    with pytest.raises(ValueError, match="not part of session"):
        svc.telemetry(sid, "44")


def test_import_selects_drivers_by_number(engine, tmp_path):
    repo = HistoricalRepository(engine, tmp_path / "c")
    s = _fake_session()
    s.get_driver = lambda code: SimpleNamespace(Abbreviation=code, DriverNumber={"VER": 1, "LEC": 16}[code])
    client = _FakeClient()
    client.load_session = lambda *a, **k: s
    m = import_session(client, repo, {"year": 2024, "event": "Monza", "session": "Q", "drivers": ["16"]})
    assert [d["code"] for d in m["drivers"]] == ["LEC"]


# ------------------------------------------------------------------ weather sampling
def test_weather_keeps_sparse_sampling_and_does_not_upsample():
    wx = pd.DataFrame([{"Time": timedelta(seconds=t), "AirTemp": 20.0 + t / 600, "TrackTemp": 30.0} for t in (0, 60, 180, 600)])
    s = _fake_session()
    s.weather_data = wx
    out = N.normalize_weather(s)
    assert out.time_s.tolist() == [0.0, 60.0, 180.0, 600.0]  # the irregular feed intervals are preserved
    assert out.humidity_pct.isna().all()
    s.weather_data = None
    assert N.normalize_weather(s).empty


# ------------------------------------------------------------------ distance alignment / comparison
def _compare_fixture(engine, tmp_path, n_a, n_b, lap_len_a=5000.0, lap_len_b=5000.0):
    svc = HistoricalService(engine, tmp_path / "c")
    sid = session_id(2024, "Monza", "Q")
    manifest = {"session_id": sid, "event": "x", "drivers": [{"code": "VER"}, {"code": "LEC"}], "channels": {}}

    def lap(n, length, t_total, start_d=0.0, start_t=1000.0):
        return pd.DataFrame({"lap_number": [1] * n, "distance_m": [start_d + length * i / (n - 1) for i in range(n)],
                             "session_time_s": [start_t + t_total * i / (n - 1) for i in range(n)], "speed_kph": [200.0] * n,
                             "throttle_pct": [100.0] * n})
    tel = {"VER": lap(n_a, lap_len_a, 80.0), "LEC": lap(n_b, lap_len_b, 81.0, start_d=12.0, start_t=5000.0)}
    svc.repo.save_session(sid, manifest, pd.DataFrame({"driver": ["VER", "LEC"], "lap_number": [1, 1]}), pd.DataFrame(), tel, {})
    return svc, sid


def test_comparison_with_different_sample_counts(engine, tmp_path):
    svc, sid = _compare_fixture(engine, tmp_path, n_a=700, n_b=413)
    r = svc.compare(sid, HistoricalCompareRequest(driver_a="VER", lap_a=1, driver_b="LEC", lap_b=1, points=50))
    assert len(r["delta"]["delta_s"]) == 50 and r["delta"]["delta_s"][0] == pytest.approx(0.0)
    assert r["delta"]["delta_s"][-1] == pytest.approx(1.0, abs=1e-6)  # 81 s vs 80 s over the lap
    assert all(b >= a - 1e-9 for a, b in zip(r["delta"]["delta_s"], r["delta"]["delta_s"][1:]))  # constant pace -> monotone delta


def test_distance_alignment_normalises_each_lap(engine, tmp_path):
    # different measured lap lengths and a non-zero distance offset are aligned on 0..100 % of each lap
    svc, sid = _compare_fixture(engine, tmp_path, n_a=100, n_b=100, lap_len_a=5000.0, lap_len_b=5050.0)
    r = svc.compare(sid, HistoricalCompareRequest(driver_a="VER", lap_a=1, driver_b="LEC", lap_b=1, points=21))
    assert r["delta"]["distance_pct"][0] == 0.0 and r["delta"]["distance_pct"][-1] == 1.0
    assert r["delta"]["delta_s"][10] == pytest.approx(0.5, abs=1e-6)


def test_comparison_with_missing_lap_is_a_clear_error(engine, tmp_path):
    svc, sid = _compare_fixture(engine, tmp_path, n_a=10, n_b=10)
    with pytest.raises(ValueError, match="telemetry missing"):
        svc.compare(sid, HistoricalCompareRequest(driver_a="VER", lap_a=2, driver_b="LEC", lap_b=1))


# ------------------------------------------------------------------ empty / incomplete sessions
def test_empty_session_is_rejected_and_nothing_is_stored(engine, tmp_path):
    repo = HistoricalRepository(engine, tmp_path / "c")
    s = _fake_session()
    s.drivers = []
    client = _FakeClient()
    client.load_session = lambda *a, **k: s
    with pytest.raises(ValueError, match="no driver list"):
        import_session(client, repo, {"year": 2024, "event": "Monza", "session": "Q"})
    s2 = _fake_session()
    s2.laps.df = s2.laps.df.iloc[0:0]
    client.load_session = lambda *a, **k: s2
    with pytest.raises(ValueError, match="no lap timing data"):
        import_session(client, repo, {"year": 2024, "event": "Monza", "session": "Q"})
    assert repo.list_sessions() == []


def test_session_without_weather_or_position_is_stored_as_partial(engine, tmp_path):
    repo = HistoricalRepository(engine, tmp_path / "c")
    s = _fake_session()
    s.weather_data = None
    client = _FakeClient()
    client.load_session = lambda *a, **k: s
    m = import_session(client, repo, {"year": 2024, "event": "Monza", "session": "Q", "drivers": ["VER"], "include_position": False})
    assert m["channels"]["weather"] is False and "no weather samples in this session" in m["warnings"]
    assert repo.weather(m["session_id"]).empty and repo.position(m["session_id"], "VER").empty
    assert "position" not in [f["kind"] for f in repo.frame_info(m["session_id"])]
