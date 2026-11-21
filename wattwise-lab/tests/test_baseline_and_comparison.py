"""Tasks 13 + 14 (WATTWISE-K4/K6): measured baseline analytics, calibration, actual-vs-optimised comparison.

Fixtures are constructed load shapes with known answers (not real household data).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from conftest import wait_done

from wattwise.adapters.meter_import import interpret
from wattwise.services import baseline as B

FLAT = {"id": "flat-test", "name": "flat test", "demand_charge_per_kw": 0.0, "periods": [{"label": "all", "start": 0, "end": 24, "price": 0.25}]}


def known_csv(days=14, start=datetime(2026, 3, 2, tzinfo=UTC)):  # 2026-03-02 is a Monday
    rows = ["timestamp,power_kw"]
    for i in range(days * 96):
        t = start + timedelta(minutes=15 * i)
        kw = 2.0 if t.weekday() >= 5 else 1.0
        if t.weekday() < 5 and 18 <= t.hour < 20:
            kw = 3.0  # weekday evening peak
        rows.append(f"{t.strftime('%Y-%m-%dT%H:%M:%SZ')},{kw}")
    return "\n".join(rows) + "\n"


def test_known_answer_baseline():
    r = interpret(known_csv())
    a = B.analyse(r.readings, "UTC", FLAT)
    assert a["coverage"] == {**a["coverage"], "days_total": 14, "days_complete": 14, "days_partial": 0}
    assert a["weekday_days"] == 10 and a["weekend_days"] == 4
    wd = 1.0 * 22 + 3.0 * 2  # 28 kWh
    we = 48.0
    assert a["daily_energy_kwh"]["mean"] == pytest.approx((10 * wd + 4 * we) / 14, abs=1e-3)
    assert a["daily_energy_kwh"]["min"] == pytest.approx(wd) and a["daily_energy_kwh"]["max"] == pytest.approx(we)
    assert a["weekday_hourly_kw"][18] == pytest.approx(3.0) and a["weekday_hourly_kw"][3] == pytest.approx(1.0)
    assert a["weekend_hourly_kw"][18] == pytest.approx(2.0)
    assert a["peak_demand"]["kw"] == 3.0 and a["peak_demand"]["averaged_over_minutes"] == 15
    assert a["baseline_cost"]["total"] == pytest.approx((10 * wd + 4 * we) * 0.25, abs=1e-6)
    assert a["provenance"] == "historical_measured" and a["baseline_cost"]["provenance"] == "historical_measured_priced"
    assert a["warnings"] == []


def test_partial_days_are_excluded_from_profiles():
    csv = known_csv(days=8)
    lines = csv.strip().split("\n")
    trimmed = "\n".join([lines[0], *lines[1 + 48:]]) + "\n"  # first day only half covered
    a = B.analyse(interpret(trimmed).readings, "UTC")
    assert a["coverage"]["days_total"] == 8 and a["coverage"]["days_complete"] == 7
    assert next(d for d in a["days"] if d["date"] == "2026-03-02")["complete"] is False
    few = B.analyse(interpret(known_csv(days=3)).readings, "UTC")
    assert "only 3 complete day(s)" in few["warnings"][0]


def test_dst_day_with_full_coverage_is_complete():
    rows = ["timestamp,power_kw"]
    t = datetime(2026, 3, 28, 0, 0, tzinfo=UTC)  # London: 29 March 2026 has 23 hours
    while t < datetime(2026, 3, 31, 0, 0, tzinfo=UTC):
        rows.append(f"{t.strftime('%Y-%m-%dT%H:%M:%SZ')},1.0")
        t += timedelta(minutes=15)
    a = B.analyse(interpret("\n".join(rows) + "\n").readings, "Europe/London")
    day = next(d for d in a["days"] if d["date"] == "2026-03-29")
    assert day["complete"] is True and day["kwh"] == pytest.approx(23.0)


def test_calibration_profile_window_order_and_notes():
    a = B.analyse(interpret(known_csv()).readings, "UTC")
    cal = B.calibration(a, ev_charger_kw=3.0)  # profile peak 2.71 kW >= 0.8 x 3.0
    prof = cal["base_profile_kw"]
    assert len(prof) == 96
    # window starts at 16:00: slot 8 = 18:00 (weekday 3 kW x10 days, weekend 2 kW x4) -> mean of complete days
    assert prof[8] == pytest.approx((10 * 3.0 + 4 * 2.0) / 14, abs=1e-4)
    assert any("counted twice" in n for n in cal["notes"]) and any("probably includes EV charging" in n for n in cal["notes"])
    with pytest.raises(ValueError):
        B.calibration(B.analyse(interpret(known_csv(days=14)).readings[:40], "UTC"))


# ---------------------------------------------------------------------------------------------- API
@pytest.fixture
def imported(client):
    m = client.post("/api/meter/import", json={"csv": known_csv(), "file_name": "known.csv", "source": "test fixture"})
    assert m.status_code == 201, m.text
    return m.json()["import_id"]


def test_summary_endpoint(client, imported):
    s = client.get(f"/api/meter/imports/{imported}/summary", params={"tariff_id": "flat-rate"}).json()
    assert s["coverage"]["days_complete"] == 14 and s["baseline_cost"]["tariff_id"] == "flat-rate"
    assert s["import"]["file_name"] == "known.csv"


def test_experiment_from_calibrated_baseline_and_comparison(client, imported):
    body = {"name": "calibrated", "experiment_id": "cal-exp", "baseline_import_id": imported, "simulator": {"latency_seconds": 0.0},
            "design": {"method": "full_factorial", "levels": 2}}
    r = client.post("/api/experiments", json=body)
    assert r.status_code == 201, r.text
    assert client.post("/api/experiments/cal-exp/execute").status_code == 202
    wait_done(client, "cal-exp")
    c = client.get("/api/comparison", params={"import_id": imported, "exp_id": "cal-exp"}).json()
    assert c["experiment"]["baseline"] == "calibrated" and c["experiment"]["calibrated_from_import"] == imported
    assert c["historical"]["mean_daily_kwh"]["provenance"] == "historical_measured"
    assert c["historical"]["mean_daily_cost"]["provenance"] == "historical_measured_priced"
    assert c["schedules"]["best_projected"]["cost"]["provenance"] == "projected"
    sv = c["savings"]["projected_vs_measured_history"]
    assert sv["provenance"] == "projected" and sv["value"] is not None
    assert c["savings"]["measured_vs_measured_history"]["value"] is None  # nothing measured yet: no measured saving claimed
    assert any("measured meter readings" in a.lower() or "imported meter readings" in a.lower() for a in c["assumptions"])
    html = client.get("/api/comparison/report.html", params={"import_id": imported, "exp_id": "cal-exp"}).text
    assert "PROJECTED" in html and "MEASURED" in html and "not estimated" in html


def test_synthetic_experiment_never_claims_savings_against_real_usage(client, imported, run_experiment):
    d = run_experiment(design={"method": "full_factorial", "levels": 2})
    c = client.get("/api/comparison", params={"import_id": imported, "exp_id": d["id"]}).json()
    assert c["experiment"]["baseline"] == "synthetic"
    sv = c["savings"]["projected_vs_measured_history"]
    assert sv["value"] is None and "synthetic household" in sv["note"]
    assert any("SYNTHETIC" in a for a in c["assumptions"])


def test_measured_schedule_is_labelled_measured(client, imported):
    body = {"name": "measured", "experiment_id": "meas-exp", "baseline_import_id": imported, "simulator": {"latency_seconds": 0.0},
            "design": {"method": "full_factorial", "levels": 2}}
    d = client.post("/api/experiments", json=body).json()
    run = d["runs"][0]
    one_day = "\n".join(known_csv(days=1).strip().split("\n")) + "\n"
    r = client.post(f"/api/experiments/meas-exp/runs/{run['run_id']}/meter", json={"csv": one_day, "unit": "kW", "departure_charge_pct": 85})
    assert r.status_code == 200, r.text
    c = client.get("/api/comparison", params={"import_id": imported, "exp_id": "meas-exp"}).json()
    assert len(c["schedules"]["measured"]) == 1 and c["schedules"]["measured"][0]["cost"]["provenance"] == "measured_schedule"
    assert c["savings"]["measured_vs_measured_history"]["value"] is not None


def test_calibrated_day_simulation_and_bad_baseline(client, imported):
    syn = client.post("/api/simulate/day", json={}).json()
    cal = client.post("/api/simulate/day", json={"baseline_import_id": imported}).json()
    assert cal["metrics"]["energy_kwh"] != pytest.approx(syn["metrics"]["energy_kwh"])
    assert client.post("/api/simulate/day", json={"baseline_import_id": "mi-nope"}).status_code == 404


def test_partial_days_never_produce_a_daily_average(client, run_experiment):
    """The demo file spans 16:00 -> 15:45 next day: two partial local days, so no 'mean daily' value may be reported."""
    m = client.post("/api/meter/import", json={"csv": client.get("/api/demo/meter.csv").text, "file_name": "demo.csv"}).json()
    d = run_experiment(design={"method": "full_factorial", "levels": 2})
    c = client.get("/api/comparison", params={"import_id": m["import_id"], "exp_id": d["id"]}).json()
    assert c["historical"]["coverage"]["days_complete"] == 0
    assert c["historical"]["mean_daily_kwh"]["value"] is None and c["historical"]["mean_daily_cost"]["value"] is None
    assert all(v["historical_per_day"]["value"] is None for v in c["cost_by_period"].values())
    assert c["historical"]["total_cost"]["value"] > 0  # the period total is still reported, as a total
