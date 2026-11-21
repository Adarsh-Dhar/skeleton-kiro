"""Tasks 4 + 5: historical sessions in the root database, durable import jobs, reliability.

FastF1 is never contacted: a fake client returns the synthetic session from test_historical.
"""
from __future__ import annotations

import threading
import time

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from test_historical import _fake_session

from db import create_db_engine, migrate
from f1lab.api.app import create_app
from f1lab.integrations.historical import repository as R
from f1lab.integrations.historical.fastf1_client import FastF1Unavailable
from f1lab.integrations.historical.repository import HistoricalRepository, session_id

IMPORT = {"year": 2024, "event": "Monza", "session": "Q", "drivers": ["VER", "LEC"]}
SID = "fastf1-2024-italian-grand-prix-q"


def _repo(tmp_path):
    e = create_db_engine(tmp_path / "trialforge.db")
    migrate(e)
    return HistoricalRepository(e, tmp_path / "fastf1-cache")


def _manifest(sid, drivers=("VER",)):
    return {"session_id": sid, "provider": "fastf1", "year": 2024, "event": "Italian Grand Prix", "session_type": "Q",
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
        "new_fastf1_column": ["a", None, "c"],  # not a canonical column: preserved through extra_json
    })
    repo.save_session("s1", _manifest("s1"), laps, pd.DataFrame(), {}, {})
    back = repo.laps("s1")
    assert back.columns.tolist() == laps.columns.tolist()
    for c in laps.columns:
        assert str(back[c].dtype) == str(laps[c].dtype), c
    assert pd.isna(back.lap_number.iloc[2]) and pd.isna(back.lap_time_s.iloc[1])
    assert back.is_deleted.tolist()[:2] == [False, True] and pd.isna(back.is_deleted.iloc[2])
    assert back.deleted_reason.iloc[1] == "track limits" and back.deleted_reason.iloc[0] is None
    assert back.new_fastf1_column.tolist() == ["a", None, "c"]


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
                       "VALUES ('half', 'importing', 'fastf1', '{\"session_id\": \"half\"}', CURRENT_TIMESTAMP)"))
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
    """20 drivers x 20 laps x 700 samples = 280k telemetry rows (a full qualifying session at FastF1 car-data rates)."""
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


# ------------------------------------------------------------------------------------------- jobs (API)
class _NoCarRow(pd.Series):
    """A lap row whose car telemetry is missing (FastF1 returns an empty frame for some laps/drivers)."""

    @property
    def _constructor(self):
        return pd.Series

    def get_car_data(self):
        return pd.DataFrame()

    def get_pos_data(self):
        return self._src.get_pos_data()


class _NoCarDataFor:
    """Wraps the fake session.laps so one driver's laps have no car telemetry."""

    def __init__(self, driver, view):
        self.driver, self.view = driver, view

    def __len__(self):
        return len(self.view)

    def __getitem__(self, key):
        res = self.view[key]
        return _NoCarDataFor(self.driver, res) if hasattr(res, "iterrows") else res

    @property
    def columns(self):
        return self.view.columns

    def iterrows(self):
        for i, r in self.view.iterrows():
            if r["Driver"] == self.driver:
                row = _NoCarRow(r)
                object.__setattr__(row, "_src", r)
                yield i, row
            else:
                yield i, r


class FakeClient:
    """Stands in for FastF1Client. ``behaviour`` decides what load_session does."""

    def __init__(self, cache_dir, behaviour="ok"):
        self.cache_dir = cache_dir
        self.behaviour = behaviour
        self.loads = 0
        self.release = threading.Event()

    def get_schedule(self, year):
        if self.behaviour == "schedule_timeout":
            raise FastF1Unavailable("could not load the 2024 schedule: HTTPSConnectionPool: Read timed out. (read timeout=5)")
        return pd.DataFrame([{"EventName": "Italian Grand Prix", "Location": "Monza"}])

    def load_session(self, year, event, code, **kw):
        self.loads += 1
        b = self.behaviour
        if b == "block":
            self.release.wait(10)
        if b == "connection":
            raise ConnectionError("Max retries exceeded with url: livetiming.formula1.com")
        if b == "timeout":
            raise TimeoutError("timed out")
        if b == "unavailable":
            raise FastF1Unavailable("could not load session 2024/Italian Grand Prix/Q: session does not exist")
        if b == "fail_once":
            self.behaviour = "ok"
            raise ConnectionError("connection reset")
        sess = _fake_session()
        if b == "no_laps":
            sess.laps.df = sess.laps.df.iloc[0:0]
        if b == "no_telemetry_lec":
            sess.laps = _NoCarDataFor("LEC", sess.laps)
        (self.cache_dir / f"download-{self.loads}.ff1pkl").parent.mkdir(parents=True, exist_ok=True)
        if self.loads == 1:
            (self.cache_dir / "download-1.ff1pkl").write_bytes(b"x" * 2_000_000)  # first load "downloads" into the cache
        return sess


@pytest.fixture
def make_app(tmp_path):
    path = str(tmp_path / "trialforge.db")
    clients = []

    def _make(behaviour="ok"):
        holder = {}

        def factory():
            holder["c"] = FakeClient(tmp_path / "fastf1-cache", behaviour)
            clients.append(holder["c"])
            return holder["c"]

        return TestClient(create_app(path, fastf1_client_factory=factory)), holder

    return _make, clients


def _wait(c, jid, states=("COMPLETED", "FAILED", "CANCELLED"), timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        j = c.get(f"/api/historical/imports/{jid}").json()
        if j["state"] in states:
            return j
        time.sleep(0.02)
    raise AssertionError(f"job stuck: {j}")


def test_import_persists_and_survives_restart_without_redownload(make_app):
    make, clients = make_app
    app, holder = make()
    with app as c:
        r = c.post("/api/historical/import", json=IMPORT)
        assert r.status_code == 202 and r.json()["duplicate"] is False
        job = _wait(c, r.json()["import_id"])
        assert job["state"] == "COMPLETED" and job["session_id"] == SID
        assert "downloaded" in job["detail"]
    app2, holder2 = make()
    with app2 as c:
        assert c.get(f"/api/historical/imports/{job['import_id']}").json()["state"] == "COMPLETED"
        assert [s["session_id"] for s in c.get("/api/historical/sessions").json()] == [SID]
        t = c.get(f"/api/historical/sessions/{SID}/telemetry", params={"driver": "VER", "lap": 1}).json()
        assert t["count"] == 4
        assert len(c.get(f"/api/historical/sessions/{SID}/laps").json()) == 4
        # opening the stored session never touched FastF1
        assert "c" not in holder2
        # asking again is a duplicate, still without a download
        r = c.post("/api/historical/import", json=IMPORT)
        assert r.json()["duplicate"] is True and r.json()["import_id"] == job["import_id"]
        assert "c" not in holder2


def test_force_reimport_uses_the_fastf1_cache(make_app):
    make, _ = make_app
    app, holder = make()
    with app as c:
        _wait(c, c.post("/api/historical/import", json=IMPORT).json()["import_id"])
        r = c.post("/api/historical/import", params={"force": True}, json=IMPORT)
        assert r.json()["duplicate"] is False
        job = _wait(c, r.json()["import_id"])
        assert job["state"] == "COMPLETED" and "served from the FastF1 cache" in job["detail"]
        assert holder["c"].loads == 2  # one client (one cache) for every import
        assert holder["c"].cache_dir.name == "fastf1-cache"


def test_same_session_different_event_spelling_reuses_stored_session(make_app):
    make, _ = make_app
    app, holder = make()
    with app as c:
        _wait(c, c.post("/api/historical/import", json=IMPORT).json()["import_id"])
        job = _wait(c, c.post("/api/historical/import", json={**IMPORT, "event": "Italian Grand Prix"}).json()["import_id"])
        assert job["state"] == "COMPLETED" and "reused without downloading" in job["detail"]
        assert holder["c"].loads == 1


def test_duplicate_while_running_and_cancel(make_app):
    make, _ = make_app
    app, holder = make("block")
    with app as c:
        jid = c.post("/api/historical/import", json=IMPORT).json()["import_id"]
        _wait(c, jid, states=("DOWNLOADING",))
        dup = c.post("/api/historical/import", json=IMPORT).json()
        assert dup["duplicate"] is True and dup["import_id"] == jid
        r = c.post(f"/api/historical/imports/{jid}/cancel")
        assert r.status_code == 200 and r.json()["cancel_requested"] is True
        holder["c"].release.set()
        job = _wait(c, jid)
        assert job["state"] == "CANCELLED" and job["error_code"] == "CANCELLED"
        assert c.get("/api/historical/sessions").json() == []  # nothing stored
        assert c.post(f"/api/historical/imports/{jid}/cancel").status_code == 409


@pytest.mark.parametrize(("behaviour", "code"), [
    ("connection", "NETWORK_ERROR"), ("timeout", "NETWORK_TIMEOUT"), ("schedule_timeout", "NETWORK_TIMEOUT"),
    ("unavailable", "SESSION_UNAVAILABLE"), ("no_laps", "NO_LAP_DATA"),
])
def test_error_classes(make_app, behaviour, code):
    make, _ = make_app
    app, _h = make(behaviour)
    with app as c:
        job = _wait(c, c.post("/api/historical/import", json=IMPORT).json()["import_id"])
        assert job["state"] == "FAILED" and job["error_code"] == code, job
        assert job["error"]
        assert c.get("/api/historical/sessions").json() == []


def test_missing_driver_and_unknown_event(make_app):
    make, _ = make_app
    app, _h = make()
    with app as c:
        job = _wait(c, c.post("/api/historical/import", json={**IMPORT, "drivers": ["VER", "HAM"]}).json()["import_id"])
        assert job["error_code"] == "DRIVER_NOT_FOUND" and "HAM" in job["error"]
        job = _wait(c, c.post("/api/historical/import", json={**IMPORT, "event": "Atlantis"}).json()["import_id"])
        assert job["error_code"] == "EVENT_NOT_FOUND"
        job = _wait(c, c.post("/api/historical/import", json={**IMPORT, "session": "WARMUP"}).json()["import_id"])
        assert job["error_code"] == "INVALID_REQUEST"


def test_partial_telemetry_completes_with_warning(make_app):
    make, _ = make_app
    app, _h = make("no_telemetry_lec")
    with app as c:
        job = _wait(c, c.post("/api/historical/import", json=IMPORT).json()["import_id"])
        assert job["state"] == "COMPLETED" and "no car telemetry for LEC" in job["detail"]
        m = c.get(f"/api/historical/sessions/{SID}").json()
        assert m["partial"] is True and m["channels"]["telemetry"] == {"VER": True, "LEC": False}
        assert c.get(f"/api/historical/sessions/{SID}/telemetry", params={"driver": "LEC"}).json()["count"] == 0


def test_retry_after_failure(make_app):
    make, _ = make_app
    app, holder = make("fail_once")
    with app as c:
        first = _wait(c, c.post("/api/historical/import", json=IMPORT).json()["import_id"])
        assert first["state"] == "FAILED"
        assert c.post(f"/api/historical/imports/{first['import_id']}/retry").status_code == 202
        jobs = c.get("/api/historical/imports").json()
        retry = next(j for j in jobs if j["retry_of"] == first["import_id"])
        retry = _wait(c, retry["import_id"])
        assert retry["state"] == "COMPLETED" and retry["attempts"] == 2
        assert c.post(f"/api/historical/imports/{retry['import_id']}/retry").status_code == 409


def test_interrupted_job_is_marked_failed_on_restart(make_app, tmp_path):
    make, _ = make_app
    app, _h = make()
    with app:
        pass
    e = create_db_engine(tmp_path / "trialforge.db")
    with e.begin() as c:
        c.execute(text("INSERT INTO f1_import_job (import_id, state, detail, request_json, dedupe_key, attempts, cancel_requested, created_at, updated_at) "
                       "VALUES ('imp-x', 'DOWNLOADING', 'downloading', :r, 'k', 1, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"),
                  {"r": '{"year": 2024, "event": "Monza", "session": "Q", "drivers": []}'})
    app2, _h2 = make()
    with app2 as c:
        j = c.get("/api/historical/imports/imp-x").json()
        assert j["state"] == "FAILED" and j["error_code"] == "INTERRUPTED" and "Retry" in j["error"]


def test_unknown_import_and_session_are_404(make_app):
    make, _ = make_app
    app, _h = make()
    with app as c:
        assert c.get("/api/historical/imports/imp-nope").status_code == 404
        assert c.post("/api/historical/imports/imp-nope/retry").status_code == 404
        assert c.delete(f"/api/historical/sessions/{session_id(2024, 'x', 'Q')}").status_code == 404


# ------------------------------------------------------------------------------------------- FastF1 sign-in never blocks the app
def test_app_imports_never_prompt_for_credentials(monkeypatch):
    import io
    import sys

    from f1lab.integrations.historical import fastf1_client as C

    class FakeTTY(io.StringIO):
        def isatty(self):
            return True

    tty = FakeTTY("")
    monkeypatch.setattr(sys, "stdin", tty)
    with C._stdin_policy(interactive=False):
        assert not sys.stdin.isatty()  # FastF1 sees no terminal, so it cannot block on input()
    assert sys.stdin is tty
    with C._stdin_policy(interactive=True):
        assert sys.stdin is tty  # the command-line script may prompt


def test_missing_credentials_are_named_in_the_error(make_app, monkeypatch):
    monkeypatch.delenv("F1_USERNAME", raising=False)
    monkeypatch.delenv("F1_PASSWORD", raising=False)
    make, _ = make_app
    app, _h = make("no_laps")
    with app as c:
        job = _wait(c, c.post("/api/historical/import", json=IMPORT).json()["import_id"])
        assert job["error_code"] == "NO_LAP_DATA" and "F1_USERNAME and F1_PASSWORD" in job["error"]
