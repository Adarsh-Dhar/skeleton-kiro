"""Historical sessions in the root database: dtypes and NULLs round-trip, missing channels stay missing, saves are atomic,
incomplete sessions are never listed, deletes cascade, and a full-size session saves and reads quickly."""
from __future__ import annotations

import time

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import text

from db import create_db_engine, migrate
from f1lab.integrations.historical import repository as R
from f1lab.integrations.historical.repository import HistoricalRepository


def _repo(tmp_path):
    e = create_db_engine(tmp_path / "trialforge.db")
    migrate(e)
    return HistoricalRepository(e)


def _manifest(sid, drivers=("VER",)):
    return {"session_id": sid, "provider": "openf1", "year": 2024, "event": "Italian Grand Prix", "session_type": "Q",
            "drivers": [{"code": d, "number": str(i + 1)} for i, d in enumerate(drivers)], "channels": {}}


# ------------------------------------------------------------------------------------------- repository
def test_dtypes_nulls_and_extra_columns_round_trip(tmp_path):
    repo = _repo(tmp_path)
    laps = pd.DataFrame({
        "driver": ["VER", "VER", "LEC"],
        "lap_number": pd.array([1, 2, None], dtype="Int64"),
        "lap_time_s": [80.1, np.nan, 81.0],
        "is_deleted": pd.array([False, True, None], dtype="boolean"),
        "is_accurate": pd.array([True, None, False], dtype="boolean"),
        "deleted_reason": [None, "track limits", None],
        "compound": ["SOFT", "SOFT", None],
        "new_provider_column": ["a", None, "c"],  # not a canonical column: preserved through extra_json
    })
    repo.save_session("s1", _manifest("s1"), laps, pd.DataFrame(), {}, {})
    back = repo.laps("s1")
    assert back.columns.tolist() == laps.columns.tolist()
    for c in laps.columns:
        assert str(back[c].dtype) == str(laps[c].dtype), c
    assert pd.isna(back.lap_number.iloc[2]) and pd.isna(back.lap_time_s.iloc[1])
    assert back.is_deleted.tolist()[:2] == [False, True] and pd.isna(back.is_deleted.iloc[2])
    assert back.deleted_reason.iloc[1] == "track limits" and back.deleted_reason.iloc[0] is None
    assert back.new_provider_column.tolist() == ["a", None, "c"]


def test_missing_channels_stay_missing(tmp_path):
    repo = _repo(tmp_path)
    tel = pd.DataFrame({"lap_number": [1, 1], "distance_m": [0.0, 10.0], "speed_kph": [100.0, 110.0], "brake_applied": [0.0, 100.0]})
    repo.save_session("s2", _manifest("s2"), pd.DataFrame({"driver": ["VER"], "lap_number": [1]}), pd.DataFrame(), {"VER": tel}, {"VER": pd.DataFrame()})
    back = repo.telemetry("s2", "VER")
    assert back.columns.tolist() == ["lap_number", "distance_m", "speed_kph", "brake_applied"]  # no brake_pct/rpm/gear columns invented
    assert repo.position("s2", "VER").empty  # empty frame not stored
    assert repo.weather("s2").empty
    assert repo.telemetry("s2", "LEC").empty


def test_failed_save_leaves_nothing_and_keeps_previous_version(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    laps = pd.DataFrame({"driver": ["VER"], "lap_number": [1], "lap_time_s": [80.0]})
    tel = {"VER": pd.DataFrame({"lap_number": [1], "speed_kph": [200.0]})}
    repo.save_session("s3", _manifest("s3"), laps, pd.DataFrame(), tel, {})
    real = R.write_frame

    def boom(conn, table, df, keys, canon):
        if table.name == "f1_hist_telemetry":
            raise RuntimeError("disk full")
        return real(conn, table, df, keys, canon)

    monkeypatch.setattr(R, "write_frame", boom)
    with pytest.raises(RuntimeError):
        repo.save_session("s3", {**_manifest("s3"), "event": "NEW"}, laps.assign(lap_time_s=[1.0]), pd.DataFrame(), tel, {})
    with pytest.raises(RuntimeError):
        repo.save_session("s4", _manifest("s4"), laps, pd.DataFrame(), tel, {})
    # s3 still has the old, complete version; s4 does not exist at all
    assert repo.manifest("s3")["event"] == "Italian Grand Prix" and repo.laps("s3").lap_time_s.iloc[0] == 80.0
    assert repo.manifest("s4") is None and [s["session_id"] for s in repo.list_sessions()] == ["s3"]
    with repo.engine.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM f1_hist_lap WHERE session_id='s4'")).scalar() == 0


def test_incomplete_session_row_is_never_listed_or_opened(tmp_path):
    repo = _repo(tmp_path)
    with repo.engine.begin() as c:
        c.execute(text("INSERT INTO f1_hist_session (session_id, status, provider, manifest_json, imported_at) "
                       "VALUES ('half', 'importing', 'openf1', '{\"session_id\": \"half\"}', CURRENT_TIMESTAMP)"))
    assert repo.manifest("half") is None and repo.list_sessions() == [] and repo.laps("half").empty


def test_delete_session_cascades(tmp_path):
    repo = _repo(tmp_path)
    repo.save_session("s5", _manifest("s5"), pd.DataFrame({"driver": ["VER"]}), pd.DataFrame(),
                      {"VER": pd.DataFrame({"lap_number": [1], "speed_kph": [1.0]})}, {})
    assert repo.delete_session("s5")
    with repo.engine.connect() as c:
        for t in ("f1_hist_lap", "f1_hist_telemetry", "f1_hist_frame"):
            assert c.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() == 0


def test_monza_sized_session_performance(tmp_path):
    """20 drivers x 20 laps x 700 samples = 280k telemetry rows (a full qualifying session at a 3-4 Hz car-data rate)."""
    repo = _repo(tmp_path)
    rng = np.random.default_rng(0)
    n_lap, n_s = 20, 700
    drivers = [f"D{i:02d}" for i in range(20)]
    tel = {}
    for d in drivers:
        tel[d] = pd.DataFrame({
            "lap_number": np.repeat(np.arange(1, n_lap + 1), n_s), "distance_m": np.tile(np.linspace(0, 5793, n_s), n_lap),
            "session_time_s": np.arange(n_lap * n_s) * 0.12, "speed_kph": rng.uniform(80, 340, n_lap * n_s),
            "throttle_pct": rng.uniform(0, 100, n_lap * n_s), "brake_applied": rng.choice([0.0, 100.0], n_lap * n_s),
            "gear": rng.integers(1, 9, n_lap * n_s).astype(float), "rpm": rng.uniform(8000, 12500, n_lap * n_s),
            "drs": rng.choice([0.0, 12.0], n_lap * n_s)})
    laps = pd.DataFrame([{"driver": d, "lap_number": i} for d in drivers for i in range(1, n_lap + 1)])
    t0 = time.perf_counter()
    repo.save_session("monza", _manifest("monza", drivers), laps, pd.DataFrame(), tel, {})
    t_save = time.perf_counter() - t0
    t0 = time.perf_counter()
    one = repo.telemetry("monza", "D07")
    t_read = time.perf_counter() - t0
    assert len(one) == n_lap * n_s
    assert np.allclose(one.speed_kph.to_numpy(), tel["D07"].speed_kph.to_numpy())
    print(f"\nsave 280k rows: {t_save:.2f}s, read one driver (14k rows): {t_read:.3f}s, db size {(tmp_path / 'trialforge.db').stat().st_size / 1e6:.1f} MB")
    assert t_save < 60 and t_read < 3  # generous bounds for slow CI machines
