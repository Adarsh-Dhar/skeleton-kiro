"""OpenF1 normalizers against REAL downloaded responses (tests/fixtures/openf1/*.json, from scripts/refresh_openf1_fixtures.py).

The fixtures cover a 2023 race, a 2024 qualifying session, a 2024 sprint, a 2025 race and a 2026 race. If OpenF1 renames or adds a
field, the schema test fails and shows which one.
"""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from f1lab.integrations.historical import openf1_normalizer as N

FIX = Path(__file__).parent / "fixtures" / "openf1"
SESSIONS = {p.stem: json.loads(p.read_text()) for p in sorted(FIX.glob("*.json"))}
MK = {"meeting_key", "session_key"}

DOCUMENTED = {  # https://openf1.org/docs
    "meetings": {"circuit_key", "circuit_image", "circuit_info_url", "circuit_short_name", "circuit_type", "country_code", "country_flag", "country_key",
                 "country_name", "date_end", "date_start", "gmt_offset", "is_cancelled", "location", "meeting_key", "meeting_name",
                 "meeting_official_name", "year"},
    "sessions": {"circuit_key", "circuit_short_name", "country_code", "country_key", "country_name", "date_end", "date_start", "gmt_offset", "is_cancelled",
                 "location", "meeting_key", "session_key", "session_name", "session_type", "year"},
    "drivers": {"broadcast_name", "country_code", "driver_number", "first_name", "full_name", "headshot_url", "last_name", "name_acronym", "team_colour",
                "team_name"} | MK,
    "laps": {"date_start", "driver_number", "duration_sector_1", "duration_sector_2", "duration_sector_3", "i1_speed", "i2_speed", "is_pit_out_lap",
             "lap_duration", "lap_number", "segments_sector_1", "segments_sector_2", "segments_sector_3", "st_speed"} | MK,
    "car_data": {"brake", "date", "driver_number", "drs", "n_gear", "rpm", "speed", "throttle"} | MK,
    "location": {"date", "driver_number", "x", "y", "z"} | MK,
    "stints": {"compound", "driver_number", "lap_end", "lap_start", "stint_number", "tyre_age_at_start"} | MK,
    "pit": {"date", "driver_number", "lane_duration", "lap_number", "pit_duration", "stop_duration"} | MK,
    "weather": {"air_temperature", "date", "humidity", "pressure", "rainfall", "track_temperature", "wind_direction", "wind_speed"} | MK,
    "race_control": {"category", "date", "driver_number", "flag", "lap_number", "message", "qualifying_phase", "scope", "sector"} | MK,
    "position": {"date", "driver_number", "position"} | MK,
    "session_result": {"dnf", "dns", "dsq", "driver_number", "duration", "gap_to_leader", "number_of_laps", "position",
                       "points"} | MK,  # 'points' is returned for races but not in the docs table
    "starting_grid": {"driver_number", "lap_duration", "position"} | MK,
    "intervals": {"date", "driver_number", "gap_to_leader", "interval"} | MK,
    "overtakes": {"date", "overtaken_driver_number", "overtaking_driver_number", "position"} | MK,
    "team_radio": {"date", "driver_number", "recording_url"} | MK,
    "drivers_championship": {"driver_number", "points_current", "points_start", "position_current", "position_start"} | MK,
    "teams_championship": {"points_current", "points_start", "position_current", "position_start", "team_name"} | MK,
}


def test_fixtures_exist_for_2023_to_2026_with_sprint_and_qualifying():
    assert set(SESSIONS) >= {"2023-bahrain-race", "2024-monza-qualifying", "2024-miami-sprint", "2025-abu-dhabi-race", "2026-latest-race"}
    names = {s["sessions"][0]["session_name"] for s in SESSIONS.values()}
    assert {"Race", "Qualifying", "Sprint"} <= names
    assert {s["sessions"][0]["year"] for s in SESSIONS.values()} == {2023, 2024, 2025, 2026}


@pytest.mark.parametrize("label", sorted(SESSIONS))
def test_every_row_uses_only_documented_fields(label):
    for endpoint, rows in SESSIONS[label].items():
        if endpoint.startswith("_") or endpoint == "meeting_sessions":
            continue
        for r in rows:
            assert set(r) <= DOCUMENTED[endpoint], f"{label}/{endpoint}: undocumented field(s) {sorted(set(r) - DOCUMENTED[endpoint])}"


def _setup(label):
    d = SESSIONS[label]
    drivers = N.driver_list(d["drivers"])
    t0 = N.parse_dt(d["sessions"][0]["date_start"])
    return d, drivers, t0


@pytest.mark.parametrize("label", sorted(SESSIONS))
def test_normalizers_accept_real_responses(label):
    d, drivers, t0 = _setup(label)
    assert len(drivers) >= 20 and all(x["code"] and x["number"] for x in drivers)
    laps = N.normalize_laps(d["laps"], d["stints"], d["pit"], drivers)
    assert not laps.empty and {"i1_speed_kph", "i2_speed_kph", "st_speed_kph"} <= set(laps.columns)
    assert len(laps) == len(d["laps"])
    pos = N.race_position(d["position"], drivers, t0)
    assert pos and all(1 <= p["position"] <= 24 and p["driver"] for p in pos)
    res = N.session_result(d["session_result"], drivers)
    assert len(res) == len(d["session_result"]) and all(isinstance(r["dnf"], bool) for r in res)
    classified = [r["position"] for r in res if r["position"] is not None]
    assert [r["position"] for r in res][: len(classified)] == sorted(classified)  # unclassified (null position) drivers sort last
    assert all(r["position"] is not None or r["dnf"] or r["dns"] or r["dsq"] for r in res)  # a null position always has a reason
    for ds, fn in (("team_radio", N.team_radio), ("overtakes", N.overtakes), ("intervals", N.intervals)):
        for row in fn(d[ds], drivers, t0):
            assert row["date"] and row["time_s"] is not None
    assert all(r["recording_url"].startswith("https://") for r in N.team_radio(d["team_radio"], drivers, t0))
    wx = N.normalize_weather(d["weather"], t0)
    assert {"air_temp_c", "rainfall_flag"} <= set(wx.columns) and "rainfall_mm" not in wx.columns
    assert len(N.race_control(d["race_control"], t0)) == len(d["race_control"])
    assert len(N.pit_stops(d["pit"], drivers)) == len(d["pit"])  # (2023 Bahrain: OpenF1 published no pit rows at all)


@pytest.mark.parametrize("label", sorted(SESSIONS))
def test_real_car_and_location_samples_become_lap_assigned_frames(label):
    d, drivers, t0 = _setup(label)
    N.lap_windows(d["laps"])  # real (trimmed, null-containing) lap rows are accepted
    stamps = sorted(N.parse_dt(r["date"]) for r in d["car_data"])
    wins = [(2, stamps[0], stamps[-1] + timedelta(seconds=1))]  # the fixture holds 30 s of lap 2, not the whole lap table
    car = N.car_frame(d["car_data"], wins, t0)
    loc = N.location_frame(d["location"], wins, t0)
    # the 30 s window starts on the driver's second lap, so samples fall inside lap windows
    assert not car.empty and set(car["lap_number"]) <= {w[0] for w in wins}
    assert car["speed_kph"].between(0, 400).all() and car["brake_applied"].isin([0.0, 100.0]).all()
    assert (car["distance_m"] >= 0).all() and car["distance_m"].is_monotonic_increasing
    assert not loc.empty and {"x_m", "y_m", "z_m"} <= set(loc.columns)
    assert set(car["drs"].dropna().astype(int)) <= set(N.DRS_STATES)  # every real DRS code is a documented one


def test_some_real_sessions_have_no_pit_rows_and_that_stays_empty_not_invented():
    d, drivers, _ = _setup("2023-bahrain-race")
    assert d["pit"] == [] and d["stints"]  # stints show tyre changes, but OpenF1 published no pit-lane timings
    assert N.pit_stops(d["pit"], drivers) == []


def test_lap_times_exist_in_real_laps():
    assert any(N.normalize_laps(SESSIONS[k]["laps"], SESSIONS[k]["stints"], SESSIONS[k]["pit"], N.driver_list(SESSIONS[k]["drivers"]))["lap_time_s"].notna().any()
               for k in SESSIONS)


def test_sprint_weekend_grid_comes_from_sprint_qualifying_and_race_grid_from_qualifying():
    sprint = SESSIONS["2024-miami-sprint"]
    gs = N.grid_session(sprint["meeting_sessions"], "S")
    assert gs["session_name"] == "Sprint Qualifying"
    assert N.grid_session(sprint["meeting_sessions"], "R")["session_name"] == "Qualifying"
    assert len(sprint["starting_grid"]) == 20 and {r["session_key"] for r in sprint["starting_grid"]} == {gs["session_key"]}
    assert N.grid_session(SESSIONS["2024-monza-qualifying"]["meeting_sessions"], "Q") is None  # qualifying itself has no grid


def test_qualifying_results_keep_per_phase_arrays_and_race_results_keep_points():
    d, drivers, _ = _setup("2024-monza-qualifying")
    q = N.session_result(d["session_result"], drivers)
    assert isinstance(q[0]["duration"], list) and len(q[0]["duration"]) == 3 and q[0]["gap_to_leader_s"] is None
    r = N.session_result(*[SESSIONS["2025-abu-dhabi-race"][k] for k in ("session_result", "drivers")][:1], N.driver_list(SESSIONS["2025-abu-dhabi-race"]["drivers"]))
    assert r[0]["position"] == 1 and r[0]["points"] == 25.0 and isinstance(r[0]["duration"], float)


def test_championship_endpoints_were_empty_in_every_real_session_so_the_import_must_say_so():
    assert all(not s["drivers_championship"] and not s["teams_championship"] for s in SESSIONS.values())
    assert N.drivers_championship([], []) == [] and N.teams_championship([]) == []


def test_championship_normalizers_follow_the_documented_fields():
    drv = [{"code": "VER", "number": "1", "name": "x", "team": "y"}]
    rows = [{"driver_number": 1, "points_start": 100, "points_current": 125, "position_start": 2, "position_current": 1},
            {"driver_number": 99, "points_start": None, "points_current": None, "position_start": None, "position_current": None}]
    out = N.drivers_championship(rows, drv)
    assert out[0] == {"driver": "VER", "points_start": 100.0, "points_current": 125.0, "position_start": 2, "position_current": 1}
    assert out[1]["driver"] == "99" and out[1]["position_current"] is None  # unknown driver number keeps its number, nulls stay null
    team = N.teams_championship([{"team_name": "Ferrari", "points_start": 1, "points_current": 2, "position_start": 3, "position_current": 2}])
    assert team == [{"team": "Ferrari", "points_start": 1.0, "points_current": 2.0, "position_start": 3, "position_current": 2}]


def test_gaps_handle_numbers_lapped_text_lists_and_nulls():
    assert N._gap(1.5) == (1.5, None) and N._gap("+1 LAP") == (None, "+1 LAP") and N._gap(None) == (None, None)
    assert N._gap([1, 2, 3]) == (None, None) and N._gap(0) == (0.0, None)


def test_null_fields_in_real_laps_do_not_break_normalization():
    seen_null = False
    for label in SESSIONS:
        d, drivers, _ = _setup(label)
        df = N.normalize_laps(d["laps"], d["stints"], d["pit"], drivers)
        assert len(df) == len(d["laps"])
        seen_null |= bool(df["lap_time_s"].isna().any()) and any(r["date_start"] is None or r["lap_duration"] is None for r in d["laps"])
    assert seen_null  # the fixtures include real laps with null durations/start times (out laps, in laps)
