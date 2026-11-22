"""OpenF1 historical provider. Everything runs against the fake server in openf1_fakes; the live test is opt-in."""
from __future__ import annotations

import os
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from openf1_fakes import LAP1, FakeOpenF1, fake_client

from db import create_db_engine, migrate
from f1lab.api.app import create_app
from f1lab.integrations.historical import openf1_normalizer as N
from f1lab.integrations.historical.importer import (
    DRIVER_NOT_FOUND,
    EVENT_NOT_FOUND,
    NETWORK_ERROR,
    NETWORK_TIMEOUT,
    NO_LAP_DATA,
    SESSION_UNAVAILABLE,
    HistoricalImportError,
    ImportCancelled,
)
from f1lab.integrations.historical.openf1_client import OpenF1Client, OpenF1Error, RateLimiter, build_url
from f1lab.integrations.historical.openf1_importer import import_session_openf1
from f1lab.integrations.historical.repository import HistoricalRepository, session_id

PAYLOAD = {"year": 2025, "event": "Abu Dhabi", "session": "R"}
SID = "openf1-2025-abu-dhabi-grand-prix-r"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.now += s


# ------------------------------------------------------------------------------------------- rate limiter
def test_limiter_allows_three_per_second():
    c = Clock()
    lim = RateLimiter(per_second=3, per_minute=1000, clock=c, sleep=c.sleep)
    for _ in range(3):
        lim.acquire()
    assert c.slept == []
    lim.acquire()  # the fourth must wait for the first to leave the one-second window
    assert sum(c.slept) >= 1.0 - 1e-9


def test_limiter_allows_thirty_per_minute():
    c = Clock()
    lim = RateLimiter(per_second=1000, per_minute=30, clock=c, sleep=c.sleep)
    for _ in range(30):
        lim.acquire()
    assert c.slept == []
    lim.acquire()
    assert sum(c.slept) >= 60.0 - 1e-9


def test_limiter_cancel_stops_waiting():
    c = Clock()
    lim = RateLimiter(per_second=1, per_minute=1000, clock=c, sleep=c.sleep)
    lim.acquire()
    with pytest.raises(ImportCancelled):
        lim.acquire(cancel=lambda: True)


# ------------------------------------------------------------------------------------------- client
def test_build_url_encodes_filters_and_ranges():
    url = build_url("car_data", {"session_key": 99, "driver_number": 44}, (("date", ">=", "2025-12-07T13:10:00"), ("date", "<", "2025-12-07T13:40:00")))
    assert url == ("https://api.openf1.org/v1/car_data?driver_number=44&session_key=99"
                   "&date>=2025-12-07T13:10:00&date<2025-12-07T13:40:00")


def test_client_caches_non_empty_answers_on_disk(tmp_path):
    client, fake = fake_client(tmp_path / "c")
    assert len(client.get("meetings", {"year": 2025})) == 3
    assert len(fake.calls) == 1
    again, fake2 = fake_client(tmp_path / "c")  # a new client shares the disk cache
    assert len(again.get("meetings", {"year": 2025})) == 3
    assert fake2.calls == [] and again.cache_hits == 1


def test_empty_answer_is_not_cached_and_404_no_results_is_empty(tmp_path):
    client, fake = fake_client(tmp_path / "c")
    assert client.get("laps", {"session_key": 12345}) == []
    assert client.get("laps", {"session_key": 12345}) == []
    assert len(fake.calls) == 2


def test_retries_429_and_honours_retry_after(tmp_path):
    slept: list[float] = []
    client, fake = fake_client(tmp_path / "c", sleep=slept.append)
    fake.fail_next = [(429, {"retry-after": "3"}, b"slow down"), (503, {}, b"")]
    assert len(client.get("meetings", {"year": 2025})) == 3
    assert len(fake.calls) == 3
    assert sum(slept[:6]) == pytest.approx(3.0)  # Retry-After for the 429
    assert sum(slept) == pytest.approx(3.0 + 4.0)  # then exponential back-off (2 s * 2) for the 503


def test_gives_up_after_retries_with_a_clear_code(tmp_path):
    client, fake = fake_client(tmp_path / "c", retries=2)
    fake.fail_next = [(503, {}, b"")] * 5
    with pytest.raises(OpenF1Error) as e:
        client.get("meetings", {"year": 2025})
    assert e.value.code == NETWORK_ERROR and len(fake.calls) == 3


def test_timeout_and_connection_errors_are_classified(tmp_path):
    def timeout(url, t):
        raise TimeoutError("slow")

    def down(url, t):
        raise ConnectionError("refused")

    c1 = OpenF1Client(tmp_path / "a", fetch=timeout, retries=1, sleep=lambda s: None, limiter=RateLimiter(1000, 100000))
    with pytest.raises(OpenF1Error) as e:
        c1.get("meetings", {"year": 2025})
    assert e.value.code == NETWORK_TIMEOUT
    c2 = OpenF1Client(tmp_path / "b", fetch=down, retries=1, sleep=lambda s: None, limiter=RateLimiter(1000, 100000))
    with pytest.raises(OpenF1Error) as e:
        c2.get("meetings", {"year": 2025})
    assert e.value.code == NETWORK_ERROR


def test_client_error_is_not_retried(tmp_path):
    client, fake = fake_client(tmp_path / "c")
    fake.fail_next = [(422, {}, b'{"detail":"bad filter"}')]
    with pytest.raises(OpenF1Error) as e:
        client.get("meetings", {"year": 2025})
    assert e.value.code == SESSION_UNAVAILABLE and len(fake.calls) == 1


def test_cancel_during_backoff(tmp_path):
    client, fake = fake_client(tmp_path / "c")
    fake.fail_next = [(429, {}, b"")]
    with pytest.raises(ImportCancelled):
        client.get("meetings", {"year": 2025}, cancel=lambda: len(fake.calls) >= 1)


# ------------------------------------------------------------------------------------------- normalizer
def test_car_frame_derives_distance_and_stores_brake_as_applied_flag():
    laps = [{"driver_number": 44, "lap_number": 1, "date_start": LAP1.isoformat(), "lap_duration": 10.0}]
    wins = N.lap_windows(laps)[44]
    rows = [{"date": (LAP1 + timedelta(seconds=s)).isoformat(), "speed": 360, "throttle": 100, "brake": b, "n_gear": 8, "rpm": 12000, "drs": 0}
            for s, b in ((0, 0), (1, 100), (2, 0))]
    rows.append({"date": (LAP1 + timedelta(seconds=500)).isoformat(), "speed": 100})  # outside every lap
    df = N.car_frame(rows, wins, LAP1)
    assert len(df) == 3 and set(df["lap_number"]) == {1}
    assert df["distance_m"].tolist() == [0.0, 100.0, 200.0]  # 360 km/h = 100 m/s
    assert df["brake_applied"].tolist() == [0.0, 100.0, 0.0]
    assert "brake_pct" not in df.columns and "steer_pct" not in df.columns
    assert df["session_time_s"].tolist() == [0.0, 1.0, 2.0]


def test_range_ends_round_up_so_the_last_fraction_of_a_second_is_requested():
    from datetime import UTC, datetime
    t = datetime(2025, 12, 7, 13, 14, 32, 300000, tzinfo=UTC)
    assert N.api_time(t) == "2025-12-07T13:14:32" and N.api_time(t, ceil=True) == "2025-12-07T13:14:33"
    assert N.api_time(t.replace(microsecond=0), ceil=True) == "2025-12-07T13:14:32"


def test_distance_is_not_integrated_across_a_data_gap():
    wins = N.lap_windows([{"driver_number": 1, "lap_number": 1, "date_start": LAP1.isoformat(), "lap_duration": 100.0}])[1]
    rows = [{"date": (LAP1 + timedelta(seconds=s)).isoformat(), "speed": 360} for s in (0, 1, 30, 31)]
    assert N.car_frame(rows, wins, LAP1)["distance_m"].tolist() == [0.0, 100.0, 100.0, 200.0]


def test_location_drops_no_signal_samples_and_keeps_raw_units():
    wins = N.lap_windows([{"driver_number": 1, "lap_number": 1, "date_start": LAP1.isoformat(), "lap_duration": 100.0}])[1]
    rows = [{"date": (LAP1 + timedelta(seconds=s)).isoformat(), "x": x, "y": y, "z": z} for s, (x, y, z) in enumerate([(0, 0, 0), (5, 6, 7)])]
    df = N.location_frame(rows, wins, LAP1)
    assert df[["x_m", "y_m", "z_m"]].values.tolist() == [[5.0, 6.0, 7.0]]


def test_laps_get_stint_compound_tyre_life_and_pit_flags():
    drivers = [{"code": "HAM", "number": "44", "team": "Ferrari", "name": "x"}]
    laps = [{"driver_number": 44, "lap_number": n, "lap_duration": 90.0 + n, "is_pit_out_lap": n == 1} for n in (1, 2, 3)]
    stints = [{"driver_number": 44, "stint_number": 2, "lap_start": 1, "lap_end": 3, "compound": "SOFT", "tyre_age_at_start": 4}]
    df = N.normalize_laps(laps, stints, [{"driver_number": 44, "lap_number": 3}], drivers)
    assert df["tyre_life"].tolist() == [4, 5, 6] and set(df["compound"]) == {"SOFT"} and df["stint"].tolist() == [2, 2, 2]
    assert df["pits"].tolist() == [True, False, True]
    assert not {"is_valid", "is_deleted", "is_accurate"} & set(df.columns)  # OpenF1 does not provide these


# ------------------------------------------------------------------------------------------- importer
def _repo(tmp_path):
    e = create_db_engine(tmp_path / "trialforge.db")
    migrate(e)
    return HistoricalRepository(e, tmp_path / "fastf1-cache")


def test_import_stores_provider_labelled_partial_session(tmp_path):
    repo = _repo(tmp_path)
    client, fake = fake_client(tmp_path / "openf1-cache")
    m = import_session_openf1(client, repo, PAYLOAD)
    assert m["session_id"] == SID == session_id(2025, "Abu Dhabi Grand Prix", "R", "openf1")
    assert m["provider"] == "openf1" and m["source_label"] == "HISTORICAL F1 · OPENF1"
    assert m["corners"] == [] and m["derived"] and m["not_provided"]
    assert m["channels"]["telemetry"] == {"HAM": True, "VER": True}
    assert m["channels"]["position"] == {"HAM": True, "VER": False}
    assert m["partial"] is True and any("VER" in w and "track position" in w for w in m["warnings"])
    assert m["coverage"]["drivers_empty"] == {"car_data": [], "location": ["VER"]}
    assert m["coverage"]["endpoints"]["laps"] == {"rows": 6, "status": "ok"}
    assert m["upstream"]["session_key"] == 99
    # stored data
    laps = repo.laps(SID)
    assert len(laps) == 6 and set(laps["driver"]) == {"HAM", "VER"}
    tel = repo.telemetry(SID, "HAM")
    assert len(tel) == 1080 and set(tel["lap_number"]) == {1, 2, 3} and tel["distance_m"].max() > 1000
    assert (tel["brake_applied"].isin([0.0, 100.0])).all()
    assert repo.position(SID, "VER").empty and len(repo.position(SID, "HAM")) == 1080
    wx = repo.weather(SID)
    assert len(wx) == 5 and "rainfall_flag" in wx.columns and "rainfall_mm" not in wx.columns
    assert m["pit_stops"] == [{"driver": "HAM", "lap_number": 3, "date": m["pit_stops"][0]["date"], "lane_duration_s": 22.5, "stop_duration_s": 2.4}]
    assert m["race_control"][0]["flag"] == "GREEN"


def test_car_data_is_requested_in_30_minute_windows(tmp_path):
    client, fake = fake_client(tmp_path / "c")
    import_session_openf1(client, _repo(tmp_path), {**PAYLOAD, "drivers": ["HAM"], "include_position": False})
    car = [u for u in fake.calls if "/car_data" in u]
    assert len(car) == 1 and "date>=2025-12-07T13:10:00" in car[0] and "date<2025-12-07T13:14:33" in car[0]
    assert not any("/location" in u for u in fake.calls)


def test_reimport_is_served_from_cache_and_skipped_when_covered(tmp_path):
    repo = _repo(tmp_path)
    client, fake = fake_client(tmp_path / "c")
    import_session_openf1(client, repo, PAYLOAD)
    sent = len(fake.calls)
    second, fake2 = fake_client(tmp_path / "c")
    again = import_session_openf1(second, repo, PAYLOAD, force=True)  # forced: only the empty answer (VER location) is asked again
    assert again["session_id"] == SID and len(fake2.calls) <= 3 < sent
    third, fake3 = fake_client(tmp_path / "c")
    reused = import_session_openf1(third, repo, PAYLOAD, force=False)
    assert reused["reused"] is True
    assert [u for u in fake3.calls if "/car_data" in u or "/location" in u or "/laps" in u] == []


def test_driver_selection_and_errors(tmp_path):
    repo = _repo(tmp_path)
    client, _ = fake_client(tmp_path / "c")
    m = import_session_openf1(client, repo, {**PAYLOAD, "drivers": ["44"]})
    assert [d["code"] for d in m["drivers"]] == ["HAM"] and m["all_drivers"] is False
    with pytest.raises(HistoricalImportError) as e:
        import_session_openf1(client, repo, {**PAYLOAD, "drivers": ["XXX"]})
    assert e.value.code == DRIVER_NOT_FOUND
    with pytest.raises(HistoricalImportError) as e:
        import_session_openf1(client, repo, {**PAYLOAD, "event": "Atlantis"})
    assert e.value.code == EVENT_NOT_FOUND
    with pytest.raises(HistoricalImportError) as e:
        import_session_openf1(client, repo, {**PAYLOAD, "session": "S"})
    assert e.value.code == SESSION_UNAVAILABLE
    with pytest.raises(HistoricalImportError):
        import_session_openf1(client, repo, {**PAYLOAD, "year": 2022})


def test_no_laps_fails_and_stores_nothing(tmp_path):
    repo = _repo(tmp_path)
    client, fake = fake_client(tmp_path / "c")
    fake.drop_laps = True
    with pytest.raises(HistoricalImportError) as e:
        import_session_openf1(client, repo, PAYLOAD)
    assert e.value.code == NO_LAP_DATA and repo.list_sessions() == []


def test_failed_optional_endpoint_gives_a_partial_import_not_a_failure(tmp_path):
    repo = _repo(tmp_path)
    client, fake = fake_client(tmp_path / "c", retries=0)
    fake.fail_endpoint["weather"] = (500, b"boom")
    m = import_session_openf1(client, repo, PAYLOAD)
    assert m["partial"] is True and m["coverage"]["endpoints"]["weather"]["status"] == "error"
    assert m["channels"]["weather"] is False and repo.exists(SID)


def test_cancel_stores_nothing(tmp_path):
    repo = _repo(tmp_path)
    client, fake = fake_client(tmp_path / "c")
    with pytest.raises(ImportCancelled):
        import_session_openf1(client, repo, PAYLOAD, should_cancel=lambda: len(fake.calls) >= 6)
    assert repo.list_sessions() == []


# ------------------------------------------------------------------------------------------- API
@pytest.fixture
def api(tmp_path):
    fake = FakeOpenF1()
    with TestClient(create_app(str(tmp_path / "trialforge.db"),
                               openf1_client_factory=lambda: fake_client(tmp_path / "openf1-cache", fake)[0])) as c:
        c.fake = fake
        yield c


def _wait(c, jid, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        j = c.get(f"/api/historical/imports/{jid}").json()
        if j["state"] in ("COMPLETED", "FAILED", "CANCELLED"):
            return j
        time.sleep(0.05)
    raise AssertionError("import did not finish")


def test_event_listing_skips_testing_and_numbers_rounds(api):
    ev = api.get("/api/historical/events", params={"year": 2025, "provider": "openf1"}).json()
    assert [e["event"] for e in ev] == ["Bahrain Grand Prix", "Abu Dhabi Grand Prix"]
    assert [e["round"] for e in ev] == [1, 2]
    assert [(s["type"], s["name"]) for s in ev[1]["sessions"]] == [("Q", "Qualifying"), ("R", "Race")]
    sess = api.get("/api/historical/event-sessions", params={"year": 2025, "event": "Abu Dhabi", "provider": "openf1"}).json()
    assert [s["type"] for s in sess] == ["Q", "R"]
    drv = api.get("/api/historical/drivers", params={"year": 2025, "event": "Abu Dhabi", "session": "R", "provider": "openf1"}).json()
    assert {d["code"] for d in drv} == {"HAM", "VER"}


def test_api_flow_import_browse_and_dedupe(api):
    r = api.post("/api/historical/import", json={**PAYLOAD, "provider": "openf1"})
    assert r.status_code == 202, r.text
    job = _wait(api, r.json()["import_id"])
    assert job["state"] == "COMPLETED", job
    assert job["session_id"] == SID and "OpenF1 cache" in job["detail"] and "warnings" in job["detail"]
    dup = api.post("/api/historical/import", json={**PAYLOAD, "provider": "openf1"}).json()
    assert dup["duplicate"] is True and dup["import_id"] == job["import_id"]
    listing = api.get("/api/historical/sessions").json()
    assert [(s["session_id"], s["provider"]) for s in listing] == [(SID, "openf1")]
    m = api.get(f"/api/historical/sessions/{SID}").json()
    assert m["source_label"] == "HISTORICAL F1 · OPENF1" and m["coverage"]["endpoints"]["car_data"]["status"] == "ok"
    laps = api.get(f"/api/historical/sessions/{SID}/laps", params={"driver": "HAM"}).json()
    assert len(laps) == 3 and laps[1]["compound"] == "MEDIUM"
    tel = api.get(f"/api/historical/sessions/{SID}/telemetry", params={"driver": "ham", "lap": 2}).json()
    assert tel["count"] == 360 and "brake_applied" in tel["samples"][0] and "steer_pct" not in tel["samples"][0]
    assert api.get(f"/api/historical/sessions/{SID}/position", params={"driver": "VER"}).json()["count"] == 0


def test_fastf1_and_openf1_sessions_do_not_collide():
    assert session_id(2025, "Abu Dhabi Grand Prix", "R") == "fastf1-2025-abu-dhabi-grand-prix-r"
    assert session_id(2025, "Abu Dhabi Grand Prix", "R", "openf1") == SID


def test_openf1_before_2023_is_rejected_by_the_api(api):
    r = api.post("/api/historical/import", json={**PAYLOAD, "year": 2022, "provider": "openf1"})
    assert r.status_code == 422 and "2023" in r.text


def test_unknown_provider_is_rejected(api):
    assert api.post("/api/historical/import", json={**PAYLOAD, "provider": "other"}).status_code == 422
    assert api.get("/api/historical/events", params={"year": 2025, "provider": "other"}).status_code == 422


def test_upstream_failure_when_browsing_is_a_502_with_a_code(tmp_path):
    fake = FakeOpenF1()
    fake.fail_endpoint["meetings"] = (500, b"down")
    with TestClient(create_app(str(tmp_path / "t.db"), openf1_client_factory=lambda: fake_client(tmp_path / "c", fake, retries=0)[0])) as c:
        r = c.get("/api/historical/events", params={"year": 2025, "provider": "openf1"})
        assert r.status_code == 502 and r.json()["code"] == NETWORK_ERROR


def test_failed_job_carries_the_error_code(api):
    api.fake.drop_laps = True
    r = api.post("/api/historical/import", json={**PAYLOAD, "provider": "openf1"})
    job = _wait(api, r.json()["import_id"])
    assert job["state"] == "FAILED" and job["error_code"] == NO_LAP_DATA


def test_default_provider_is_fastf1_and_keys_are_unchanged():
    from f1lab.integrations.historical.jobs import dedupe_key
    from f1lab.integrations.historical.schemas import HistoricalImportRequest

    req = HistoricalImportRequest(year=2024, event="Monza", session="Q")
    assert req.provider == "fastf1"
    assert dedupe_key(req.model_dump()) == "2024|monza|Q|*|111"
    assert dedupe_key({**req.model_dump(), "provider": "openf1"}) == "openf1|2024|monza|Q|*|111"


# ------------------------------------------------------------------------------------------- live (opt-in)
@pytest.mark.skipif(not os.environ.get("RUN_NETWORK_TESTS"), reason="set RUN_NETWORK_TESTS=1 to contact api.openf1.org")
def test_live_openf1_one_driver_one_race(tmp_path):
    repo = _repo(tmp_path)
    client = OpenF1Client(tmp_path / "openf1-cache")
    m = import_session_openf1(client, repo, {"year": 2024, "event": "Monza", "session": "Q", "drivers": ["VER"]})
    assert m["provider"] == "openf1" and m["channels"]["laps"] is True
    assert not repo.laps(m["session_id"]).empty
