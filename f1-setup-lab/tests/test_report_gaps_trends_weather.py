"""Historical report: finishing gaps, standings reason, observed lap-time trend wording, weather sensor dropouts."""
from __future__ import annotations

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from f1lab.api.app import create_app
from f1lab.integrations.historical import analysis

RESULTS = [
    {"driver": "VER", "position": 1, "laps": 50, "points": 25.0, "dnf": False, "dns": False, "dsq": False,
     "gap_to_leader": 0, "gap_to_leader_s": 0.0, "gap_to_leader_text": None},
    {"driver": "NOR", "position": 2, "laps": 50, "points": 18.0, "dnf": False, "dns": False, "dsq": False,
     "gap_to_leader": 0.639, "gap_to_leader_s": 0.639, "gap_to_leader_text": None},
    {"driver": "HAM", "position": 15, "laps": 49, "points": 0.0, "dnf": False, "dns": False, "dsq": False,
     "gap_to_leader": "+1 LAP", "gap_to_leader_s": None, "gap_to_leader_text": "+1 LAP"},
    {"driver": "LIN", "position": None, "laps": 19, "points": 0.0, "dnf": True, "dns": False, "dsq": False,
     "gap_to_leader": None, "gap_to_leader_s": None, "gap_to_leader_text": None},
]


def test_gap_display_uses_seconds_text_or_blank():
    assert [analysis.gap_display(r) for r in RESULTS] == ["leader", "+0.639 s", "+1 LAP", ""]


def test_report_results_show_numeric_finishing_gaps():
    html = analysis.report_html({"event": "GP"}, RESULTS)
    assert "<th>gap_to_leader</th>" in html
    assert "<td>leader</td>" in html and "<td>+0.639 s</td>" in html and "<td>+1 LAP</td>" in html


def test_trend_text_never_claims_the_tyre_improved():
    fell = analysis.trend_text(-0.09, 0.814)
    assert "lap times fell 0.090 s/lap" in fell and "R\u00b2 0.81" in fell and "not evidence that the tyre improved" in fell
    assert "rose" in analysis.trend_text(0.12, 0.6) and "no clear trend" in analysis.trend_text(0.008, 0.004) and "flat" in analysis.trend_text(0.001) and analysis.trend_text(None) == ""
    assert "observed lap-time trends" in analysis.FUEL_CAVEAT and analysis.FUEL_CAVEAT.startswith("Fuel burn is not corrected")


def test_report_labels_the_slope_as_an_observed_trend():
    stints = {"stints": [{"driver": "VER", "stint": 2, "compound": "HARD", "slope_s_per_lap": -0.09, "r_squared": 0.814,
                          "laps_used": 20, "laps_excluded": {}}], "caveat": analysis.FUEL_CAVEAT}
    html = analysis.report_html({"event": "GP"}, None, None, stints)
    assert "observed lap-time trend" in html.lower() and "<th>observed_trend</th>" in html and "not evidence that the tyre improved" in html


def test_weather_dropout_zero_is_masked_but_real_values_are_kept():
    df = pd.DataFrame({"air_temp_c": [12.4, 12.4, 12.4, -3.0, 0.0], "track_temp_c": [14.4, 0.0, 14.3, 0.0, 0.0],
                       "pressure_hpa": [1000.0, 1000.0, 0.0, 990.0, 990.0]})
    out, masked = analysis.mask_weather_dropouts(df)
    assert masked == {"track_temp_c": 1, "pressure_hpa": 1}
    assert pd.isna(out.loc[1, "track_temp_c"]) and out.loc[0, "track_temp_c"] == 14.4
    assert out.loc[3, "track_temp_c"] == 0.0 and out.loc[4, "track_temp_c"] == 0.0  # freezing air: a real 0.0 stays
    assert df.loc[1, "track_temp_c"] == 0.0  # input untouched


@pytest.fixture
def api(tmp_path):
    app = create_app(str(tmp_path / "trialforge.db"))
    with TestClient(app) as c:
        svc = app.state.historical
        manifest = {"session_id": "s1", "provider": "openf1", "source_type": "external_provider", "verification": "verified", "year": 2026, "event": "Test GP", "session_type": "R",
                    "session_date": "2026-11-22", "drivers": [{"code": "VER", "number": "1", "team": "RB"}], "channels": {},
                    "coverage": {"endpoints": {
                        "drivers_championship": {"endpoint": "drivers_championship", "rows": 0, "status": "empty"},
                        "teams_championship": {"endpoint": "teams_championship", "rows": 0, "status": "empty"}}}}
        laps = pd.DataFrame({"driver": ["1"], "lap_number": [1], "lap_time_s": [90.0]})
        weather = pd.DataFrame({"time_s": [0.0, 60.0, 120.0], "air_temp_c": [12.4, 12.4, 12.4], "track_temp_c": [14.4, 0.0, 14.3]})
        svc.repo.save_session("s1", manifest, laps, weather, {}, {}, {"session_result": (RESULTS, None)})
        yield c


def test_weather_route_masks_the_dropout_and_says_so(api):
    w = api.get("/api/historical/sessions/s1/weather").json()
    assert [s["track_temp_c"] for s in w["samples"]] == [14.4, None, 14.3]
    assert w["masked_dropouts"] == {"track_temp_c": 1} and "Treated as missing" in w["note"]


def test_report_end_to_end_gaps_standings_reason_and_weather_min(api):
    body = api.get("/api/historical/sessions/s1/report.html").text
    assert "+0.639 s" in body and "leader" in body
    assert "OpenF1 published no championship standings" in body and "no data stored" not in body
    row = next(r for r in body.split("<tr>") if "Track temperature" in r)
    assert "<td>14.3</td>" in row and "<td>0</td>" not in row and "<td>0.0</td>" not in row
    assert "Treated as missing" in body
