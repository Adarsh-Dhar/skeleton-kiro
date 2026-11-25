"""Task 10.6: example and integration tests for wattwise-household-model-improvements (API level).

Feature: wattwise-household-model-improvements. Each test names the requirements it checks.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from conftest import wait_done

DISH = {"name": "Dish", "power_kw": 1.0, "duration_h": 1.0}
BIG = {"name": "Big", "power_kw": 6.0, "duration_h": 1.0}  # too large to be mistaken for the 2 kW evening load
IMPORT_OPTS = {"timestamp_column": "timestamp", "value_column": "import_kwh", "reading_kind": "interval_energy", "unit": "kWh"}
MONDAY = datetime(2026, 3, 2)  # 2026-03-02 is a Monday


def quarter_hour_csv(days=14, bump=None, export=False, start=MONDAY):
    """Interval-energy file with a 0.3 kW base. ``bump`` = (first_hour, hours, kW) adds a daily rectangular load."""
    rows = ["timestamp,import_kwh" + (",export_kwh" if export else "")]
    for i in range(days * 96):
        t = start + timedelta(minutes=15 * i)
        kw = 0.3
        if bump and bump[0] <= t.hour + t.minute / 60 < bump[0] + bump[1]:
            kw += bump[2]
        rows.append(f"{t.isoformat()},{kw / 4:.6f}" + (f",{0.1 if i % 4 == 0 else 0}" if export else ""))
    return "\n".join(rows) + "\n"


def do_import(client, csv, provenance=None, **opts):
    body = {"csv": csv, "options": {**IMPORT_OPTS, **opts}, "force": True}
    if provenance is not None:
        body["provenance"] = provenance
    r = client.post("/api/meter/import", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def study(client, **kw):
    return client.post("/api/experiments", json={"name": "t", "design": {"method": "ofat"}, **kw})


# ------------------------------------------------------------------ charger capacity (1.3, 1.4, 1.5)
def test_default_charger_range_is_1_4_to_11_kw(client):
    f = client.get("/api/meta").json()["factors"]["charge_power_kw"]
    assert (f["low"], f["high"]) == (1.4, 11.0)
    d = client.post("/api/simulate/day", json={}).json()
    assert max(s["ev_kw"] if "ev_kw" in s else s["ev"] for s in d["steps"]) <= 11.0 + 1e-9


@pytest.mark.parametrize("cap", [1.39, 350.01, 0, -3, 7.123])
def test_invalid_charger_capacity_is_rejected_and_stored_settings_unchanged(client, cap):
    saved = client.put("/api/household", json={"charger_power_kw": 7.4}).json()["settings"]
    r = client.put("/api/household", json={"charger_power_kw": cap})
    assert r.status_code == 422 and "charger capacity" in r.json()["detail"][0]["message"]
    assert client.get("/api/household").json()["settings"] == saved
    assert study(client, charger_capacity_kw=cap).status_code == 422


def test_charging_power_above_capacity_is_422_charger_capacity_error(client):
    r = client.post("/api/simulate/day", json={"charger_capacity_kw": 5, "setup": {"charge_power_kw": 7}})
    assert r.status_code == 422 and r.json()["detail"][0]["field"] == "charger_capacity_kw" and "exceeds the charger capacity" in r.json()["detail"][0]["message"]
    assert client.post("/api/simulate/day", json={"charger_capacity_kw": 7, "setup": {"charge_power_kw": 7}}).status_code == 200


def test_capacity_widens_the_design_range_and_designed_runs_stay_inside(client):
    d = study(client, charger_capacity_kw=22, factors={"charge_power_kw": {"low": 3.7, "high": 22}, "charge_start": {"low": 19, "high": 29}}).json()
    power = next(f for f in d["factors"] if f["name"] == "charge_power_kw")
    assert power["high"] == 22
    assert all(1.4 - 1e-9 <= r["configuration"]["charge_power_kw"] <= 22 + 1e-9 for r in d["runs"])
    over = study(client, charger_capacity_kw=5, factors={"charge_power_kw": {"low": 3.7, "high": 11}, "charge_start": {"low": 19, "high": 29}})
    assert over.status_code == 422 and "range must lie within [1.4, 5.0]" in over.json()["detail"][0]["message"]


# ------------------------------------------------------------------ no EV (2.1, 2.4)
def test_no_ev_with_one_flexible_appliance_completes_with_ofat_fallback(client):
    r = study(client, ev=None, appliances=[DISH], design={"method": "full_factorial"})
    assert r.status_code == 201, r.text
    d = r.json()
    assert [f["name"] for f in d["factors"]] == ["appliance_start"]
    client.post(f"/api/experiments/{d['id']}/execute")
    done = wait_done(client, d["id"])
    assert done["status"] == "completed" and done["counts"]["completed"] == done["runs_total"] > 0 and done["counts"]["failed"] == 0
    det = client.get(f"/api/experiments/{d['id']}").json()
    assert det["design"]["method"] == "ofat" and any("ofat" in n for n in det["notes"])
    day = client.post("/api/simulate/day", json={"ev": None, "appliances": [DISH]}).json()
    assert all(s["ev"] == 0 for s in day["steps"]) and day["metrics"]["ev_energy_kwh"] == 0


def test_no_ev_and_no_appliances_is_rejected_and_stored_household_unchanged(client):
    saved = client.put("/api/household", json={"base_load_kw": 0.5, "charger_power_kw": 7.4}).json()["settings"]
    r = client.put("/api/household", json={"ev": None, "appliances": []})
    assert r.status_code == 422 and "optimisable load" in r.json()["detail"][0]["message"]
    assert client.get("/api/household").json()["settings"] == saved
    assert study(client, ev=None, appliances=[]).status_code == 422
    assert client.get("/api/experiments").json() == []


# ------------------------------------------------------------------ provenance (3.3, 3.7, 3.8, 3.9)
@pytest.mark.parametrize("given,stored", [("real_meter", "real_meter"), ("synthetic_demo", "synthetic_demo"), ("unknown", "unknown"), (None, "unknown"), ("", "unknown")])
def test_import_stores_provenance_and_surfaces_source_label(client, given, stored):
    meta = do_import(client, quarter_hour_csv(2), given)
    assert meta["source_label"] == stored and meta["provenance"] == "historical_measured"  # C1: quantity kind unchanged
    assert client.get(f"/api/meter/imports/{meta['import_id']}").json()["source_label"] == stored
    assert client.get(f"/api/meter/imports/{meta['import_id']}/summary").json()["source_label"] == stored
    assert client.get(f"/api/meter/imports/{meta['import_id']}/calibration").json()["source_label"] == stored
    assert any(m["source_label"] == stored for m in client.get("/api/meter/imports").json())


def test_invalid_provenance_is_422_and_nothing_is_written(client):
    r = client.post("/api/meter/import", json={"csv": quarter_hour_csv(1), "options": IMPORT_OPTS, "provenance": "bogus"})
    assert r.status_code == 422 and r.json()["detail"][0]["field"] == "provenance"
    assert client.get("/api/meter/imports").json() == []


def test_demo_import_is_labelled_synthetic_demo_in_summary(client):
    meta = do_import(client, client.get("/api/demo/meter.csv").text, "synthetic_demo", timestamp_column="timestamp", value_column="power_kw", reading_kind="power", unit="kW")
    assert client.get(f"/api/meter/imports/{meta['import_id']}/summary").json()["source_label"] == "synthetic_demo"


# Requirement 3.7 / design C1: the comparison view should carry the file's source_label. It does not yet:
# comparison.build copies only hist["import"] (no source_label key) and none of hist's top-level keys.
@pytest.mark.xfail(strict=True, reason="BUG: GET /api/comparison does not expose the import's source_label (Requirement 3.7)")
def test_comparison_carries_the_import_source_label(client, run_experiment):
    meta = do_import(client, client.get("/api/demo/meter.csv").text, "synthetic_demo", timestamp_column="timestamp", value_column="power_kw", reading_kind="power", unit="kW")
    exp = run_experiment(design={"method": "ofat"})
    cmp_ = client.get("/api/comparison", params={"import_id": meta["import_id"], "exp_id": exp["id"]})
    assert cmp_.status_code == 200
    assert "synthetic_demo" in str(cmp_.json())


def test_meta_exposes_provenance_badges_and_keeps_factor_keys(client):
    m = client.get("/api/meta").json()
    b = m["provenance_badges"]
    assert set(b) == {"real_meter", "synthetic_demo", "unknown"} and all({"text", "css"} <= set(v) for v in b.values())
    assert len({v["text"] for v in b.values()}) == 3 and b["unknown"]["css"] == "prov-u"
    assert set(m["factors"]) == {"charge_start", "charge_power_kw", "appliance_start"}


# ------------------------------------------------------------------ import/export columns (9.1, 9.6)
def test_export_column_is_stored_totalled_and_exposed(client):
    meta = do_import(client, quarter_hour_csv(2, export=True), export_column="export_kwh")
    assert meta["total_import_kwh"] == pytest.approx(2 * 96 * 0.075, rel=1e-6) and meta["total_export_kwh"] == pytest.approx(48 * 0.1, rel=1e-6)
    rows = client.get(f"/api/meter/imports/{meta['import_id']}/readings", params={"limit": 4}).json()["readings"]
    assert [r["export_kwh"] for r in rows] == [0.1, 0, 0, 0] and rows[0]["export_kw"] == pytest.approx(0.4)
    plain = do_import(client, quarter_hour_csv(1))
    assert plain["total_export_kwh"] == 0


def test_bad_export_value_rejects_the_whole_file(client):
    bad = quarter_hour_csv(1, export=True).replace(",0.1\n", ",abc\n", 1)
    r = client.post("/api/meter/import", json={"csv": bad, "options": {**IMPORT_OPTS, "export_column": "export_kwh"}})
    assert r.status_code == 422 and client.get("/api/meter/imports").json() == []


# ------------------------------------------------------------------ calibration day type, billing cycle (5.x, 7.6, 7.7)
def test_calibration_day_type_selection_fallback_and_invalid(client):
    meta = do_import(client, quarter_hour_csv(2))  # Monday and Tuesday only
    i = meta["import_id"]
    wd = client.get(f"/api/meter/imports/{i}/calibration", params={"day_type": "weekday"}).json()
    assert (wd["day_type_used"], wd["fallback"], wd["days_used"]) == ("weekday", False, 2)
    we = client.get(f"/api/meter/imports/{i}/calibration", params={"day_type": "weekend"}).json()
    assert (we["day_type_used"], we["requested_day_type"], we["fallback"]) == ("all", "weekend", True)
    assert any("no complete weekend day" in n for n in we["notes"])
    assert client.get(f"/api/meter/imports/{i}/calibration").json()["day_type_used"] == "all"
    bad = client.get(f"/api/meter/imports/{i}/calibration", params={"day_type": "holiday"})
    assert bad.status_code == 422 and bad.json()["detail"][0]["field"] == "day_type"


def test_file_without_a_complete_day_is_422_no_complete_day(client):
    meta = do_import(client, quarter_hour_csv(1).split("\n", 41)[0] + "\n" + "\n".join(quarter_hour_csv(1).split("\n")[1:41]) + "\n")
    r = client.get(f"/api/meter/imports/{meta['import_id']}/calibration")
    assert r.status_code == 422 and r.json()["detail"][0]["field"] == "baseline"


def test_billing_cycle_default_endpoint(client):
    i = do_import(client, quarter_hour_csv(2))["import_id"]  # 0.3 kW * 24 h = 7.2 kWh per day
    url = f"/api/meter/imports/{i}/billing-cycle-default"
    assert client.get(url, params={"day": "2026-03-03"}).json() == {"kwh": 7.2, "notice": None}
    none = client.get(url, params={"day": "2026-04-03"}).json()
    assert none["kwh"] == 0.0 and "no default billing-cycle consumption" in none["notice"]
    assert client.get(url, params={"day": "not-a-date"}).status_code == 422
    assert client.get("/api/meter/imports/mi-missing/billing-cycle-default", params={"day": "2026-03-03"}).status_code == 404


# ------------------------------------------------------------------ overlap check (10.1, 10.3, 10.6, 10.7)
# A 2 kW load for one hour at 20:00 every day, two days of 14 give a clear rise
EVENING = (20, 1, 2.0)
TWO_KW = {"name": "Oven", "power_kw": 2.0, "duration_h": 1.0, "flexible": False, "fixed_start": 20.0}


def test_overlap_not_applicable_without_a_baseline(client):
    d = study(client, appliances=[TWO_KW, BIG]).json()
    assert d["baseline_check"]["status"] == "not_applicable"


def test_overlap_passed_when_the_baseline_does_not_contain_the_appliance(client):
    i = do_import(client, quarter_hour_csv(14))["import_id"]
    r = study(client, baseline_import_id=i, appliances=[TWO_KW, BIG])
    assert r.status_code == 201 and r.json()["baseline_check"]["status"] == "passed"
    assert client.get(f"/api/experiments/{r.json()['id']}").json()["baseline_check"]["status"] == "passed"


def test_overlap_blocked_422_with_baseline_check_and_nothing_stored(client):
    i = do_import(client, quarter_hour_csv(14, bump=EVENING))["import_id"]
    r = study(client, baseline_import_id=i, appliances=[TWO_KW, BIG])
    assert r.status_code == 422
    body = r.json()
    assert body["detail"][0]["field"] == "baseline" and "Oven" in body["detail"][0]["message"]
    assert body["baseline_check"]["status"] == "blocked" and body["baseline_check"]["overlapping"] == ["Oven"]
    assert client.get("/api/experiments").json() == []


def test_overlap_skipped_when_the_user_confirms_the_baseline_excludes_the_appliance(client):
    i = do_import(client, quarter_hour_csv(14, bump=EVENING))["import_id"]
    r = study(client, baseline_import_id=i, appliances=[TWO_KW, BIG], baseline_excludes_modelled_appliances=True)
    assert r.status_code == 201
    chk = r.json()["baseline_check"]
    assert chk["status"] == "skipped" and chk["skipped_by_user_confirmation"] is True


def test_overlap_subtracted_when_declared_in_baseline(client):
    i = do_import(client, quarter_hour_csv(14, bump=EVENING))["import_id"]
    r = study(client, baseline_import_id=i, appliances=[{**TWO_KW, "in_baseline": True}, BIG])
    assert r.status_code == 201, r.text
    chk = r.json()["baseline_check"]
    assert chk["status"] == "subtracted" and chk["appliances_subtracted"] == ["Oven"] and chk["clipped"]["count"] == 0


def test_declared_load_larger_than_baseline_is_clipped_and_reported(client):
    i = do_import(client, quarter_hour_csv(14))["import_id"]  # base is only 0.3 kW
    r = study(client, baseline_import_id=i, appliances=[{**TWO_KW, "in_baseline": True}, BIG])
    assert r.status_code == 201
    chk = r.json()["baseline_check"]
    assert chk["status"] == "subtracted" and chk["clipped"]["count"] == 4 and "20:00" in chk["clipped"]["times"]


def test_empty_baseline_blocks_the_study_with_422(client):
    i = do_import(client, quarter_hour_csv(14))["import_id"]
    with client.app.state.lab.engine.begin() as c:
        c.execute(text("DELETE FROM ww_meter_reading WHERE import_id = :i"), {"i": i})
    r = study(client, baseline_import_id=i)
    assert r.status_code == 422 and "baseline is unavailable" in r.json()["detail"][0]["message"]
    assert client.get("/api/experiments").json() == []


# ------------------------------------------------------------------ appliance start mode and factor conflict (4.x)
def test_per_appliance_mode_creates_one_factor_per_flexible_appliance(client):
    apps = [{"id": "dish", **DISH}, {"id": "wash", "name": "Wash", "power_kw": 0.5, "duration_h": 1.5}, {"name": "Dryer", "power_kw": 2, "duration_h": 1, "flexible": False}]
    d = study(client, appliances=apps, appliance_start_mode="per_appliance", ev=None, factors={"appliance_start": {"low": 18, "high": 26}}).json()
    names = [f["name"] for f in d["factors"]]
    assert names == ["appliance_start__dish", "appliance_start__wash"]
    assert all("appliance_start__dish" in r["configuration"] and "appliance_start" not in r["configuration"] for r in d["runs"])


def test_shared_mode_is_the_default(client):
    d = study(client, ev=None, appliances=[DISH, {**DISH, "name": "Other"}]).json()
    assert [f["name"] for f in d["factors"]] == ["appliance_start"]


def test_setup_with_shared_and_per_appliance_keys_is_422_factor_conflict(client):
    r = client.post("/api/simulate/day", json={"appliances": [{"id": "dish", **DISH}], "setup": {"appliance_start": 19, "appliance_start__dish": 20}})
    assert r.status_code == 422 and r.json()["detail"][0]["field"] == "appliance_start" and "both" in r.json()["detail"][0]["message"]


def test_explicit_per_appliance_start_in_a_setup_is_accepted(client):
    r = client.post("/api/simulate/day", json={"appliances": [{"id": "dish", **DISH}], "ev": None, "setup": {"appliance_start__dish": 22}})
    assert r.status_code == 200 and r.json()["setup"]["appliance_start__dish"] == 22


# ------------------------------------------------------------------ tariff dates and meter pricing (6.x, 8.1)
def test_meter_reading_across_a_period_boundary_is_split_and_priced_per_slice(client):
    tariff = {"name": "Split", "periods": [{"label": "night", "start": 0, "end": 7, "price": 0.1}, {"label": "day", "start": 7, "end": 24, "price": 0.3}]}
    csv = "timestamp,power_kw\n2026-03-03T06:30:00,2\n2026-03-03T07:30:00,2\n2026-03-03T08:30:00,2\n"
    s = client.post("/api/meter/preview", json={"csv": csv, "tariff": tariff}).json()["summary"]
    assert s["energy_kwh"] == pytest.approx(6.0) and s["interval_minutes"] == 60
    assert s["cost_by_period"]["night"]["kwh"] == pytest.approx(1.0) and s["cost_by_period"]["day"]["kwh"] == pytest.approx(5.0)
    assert s["energy_charge"] == pytest.approx(1.0 * 0.1 + 5.0 * 0.3)


def test_date_dependent_tariff_prices_the_window_by_its_start_date(client):
    t = {"name": "Wk", "periods": [{"label": "wd", "start": 0, "end": 24, "price": 0.1, "days": "weekday"},
                                   {"label": "we", "start": 0, "end": 24, "price": 0.5, "days": "weekend"}]}
    req = {"tariff": t, "ev": None, "appliances": [DISH], "base_load_kw": 0.0, "setup": {"appliance_start": 25.0}}
    fri = client.post("/api/simulate/day", json={**req, "window_start_date": "2026-03-06"}).json()  # Friday: 25h = Saturday 01:00
    mon = client.post("/api/simulate/day", json={**req, "window_start_date": "2026-03-02"}).json()  # Monday: 25h = Tuesday 01:00
    # window hour 24 and later is Saturday for the Friday start and Tuesday for the Monday start (rates 0.5 weekend, 0.1 weekday)
    def expected(day, weekend_from):
        return sum(s["kw"] * 0.25 * (0.5 if weekend_from is not None and s["t"] >= weekend_from else 0.1) for s in day["steps"])
    assert fri["metrics"]["energy_charge"] == pytest.approx(expected(fri, 24.0), rel=1e-6)
    assert mon["metrics"]["energy_charge"] == pytest.approx(expected(mon, None), rel=1e-6)
    assert fri["metrics"]["energy_charge"] > mon["metrics"]["energy_charge"]
