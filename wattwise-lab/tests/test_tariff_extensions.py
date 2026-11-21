"""Task 8 (WATTWISE-K5): tariffs in the root database + generic optional tariff extensions.

The extension tests use made-up example numbers to check the arithmetic; they are not any real utility's tariff.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from wattwise.adapters.household_model import (
    DEFAULT_APPLIANCES,
    DEFAULT_EV,
    DEFAULT_TARIFFS,
    simulate,
    summarise,
    validate_tariff,
)
from wattwise.adapters.tariff_model import bill, is_extended

# energy_cost etc. computed with the code BEFORE the extensions were added (captured once, never edited)
REGRESSION = {
    "time-of-use|19.0|7.4|19.0": (15.5877391304, 15.5877391304, 0.0), "time-of-use|23.0|3.7|25.0": (8.1635, 8.1635, 0.0),
    "time-of-use|17.0|11.0|17.0": (21.1070065409, 21.1070065409, 0.0), "flat-rate|19.0|7.4|19.0": (16.0505434783, 16.0505434783, 0.0),
    "flat-rate|23.0|3.7|25.0": (12.5, 12.5, 0.0), "flat-rate|17.0|11.0|17.0": (16.0505434783, 16.0505434783, 0.0),
    "night-saver-demand|19.0|7.4|19.0": (23.2840109797, 15.8740109797, 7.41), "night-saver-demand|23.0|3.7|25.0": (12.2125, 8.8225, 3.39),
    "night-saver-demand|17.0|11.0|17.0": (27.1476086957, 17.5776086957, 9.57),
}
HH = {"ev": DEFAULT_EV, "base_load_kw": 0.35, "appliances": DEFAULT_APPLIANCES}
FLAT = {"name": "flat", "demand_charge_per_kw": 0.0, "periods": [{"label": "all", "start": 0, "end": 24, "price": 0.2}]}


@pytest.mark.parametrize("key", sorted(REGRESSION))
def test_existing_tariffs_give_identical_costs(key):
    tid, cs, cp, ap = key.split("|")
    t = next(t for t in DEFAULT_TARIFFS if t["id"] == tid)
    assert not is_extended(t)
    m = simulate({"charge_start": float(cs), "charge_power_kw": float(cp), "appliance_start": float(ap)}, {**HH, "tariff": t})["metrics"]
    cost, charge, demand = REGRESSION[key]
    assert m["energy_cost"] == pytest.approx(cost, abs=1e-9) and m["energy_charge"] == pytest.approx(charge, abs=1e-9)
    assert m["demand_charge"] == pytest.approx(demand, abs=1e-9)


def test_period_boundaries_are_half_open():
    t = {"name": "x", "periods": [{"label": "a", "start": 0, "end": 7, "price": 0.1}, {"label": "b", "start": 7, "end": 24, "price": 0.3}]}
    s = summarise([(6.75, 4.0), (7.0, 4.0)], t)  # 06:45-07:00 is 'a', 07:00 is 'b'
    assert s["cost_by_period"]["a"]["cost"] == pytest.approx(4 * 0.25 * 0.1) and s["cost_by_period"]["b"]["cost"] == pytest.approx(4 * 0.25 * 0.3)
    wrap = {"name": "w", "periods": [{"label": "n", "start": 23, "end": 7, "price": 0.1}, {"label": "d", "start": 7, "end": 23, "price": 0.3}]}
    assert validate_tariff(wrap) == []
    assert summarise([(23.0, 1.0)], wrap)["cost_by_period"].keys() == {"n"}


def test_fixed_charges_tax_and_surcharge():
    t = {**FLAT, "fixed_daily_charge": 0.5, "fixed_monthly_charge": 9.0, "billing_days": 30, "surcharge_per_kwh": 0.05, "tax_pct": 10}
    assert validate_tariff(t) == [] and is_extended(t)
    s = summarise([(16.0, 4.0)] * 4, t)  # 4 kWh
    energy = 4 * (0.2 + 0.05)
    fixed = 0.5 + 9.0 / 30
    assert s["energy_charge"] == pytest.approx(energy) and s["fixed_charge"] == pytest.approx(fixed)
    assert s["tax"] == pytest.approx((energy + fixed) * 0.1) and s["energy_cost"] == pytest.approx((energy + fixed) * 1.1)


def test_tiers_split_inside_an_interval():
    t = {**FLAT, "tiers": [{"up_to_kwh": 3, "adder_per_kwh": 0.0}, {"up_to_kwh": None, "adder_per_kwh": 0.1}]}
    s = summarise([(16.0, 8.0), (16.25, 8.0)], t)  # 2 kWh then 2 kWh; the second interval crosses 3 kWh
    assert s["energy_charge"] == pytest.approx(3 * 0.2 + 1 * 0.3)
    assert validate_tariff({**FLAT, "tiers": [{"up_to_kwh": 5, "adder_per_kwh": 0}]})  # last tier must be open-ended
    assert validate_tariff({**FLAT, "tiers": [{"up_to_kwh": 5, "adder_per_kwh": 0}, {"up_to_kwh": 2, "adder_per_kwh": 0}, {"up_to_kwh": None, "adder_per_kwh": 0}]})


def test_export_credit_never_charges_exports_at_import_price():
    t = {**FLAT, "export_credit_per_kwh": 0.05}
    s = summarise([(12.0, -4.0), (12.25, 4.0)], t)
    assert s["energy_charge"] == pytest.approx(1 * 0.2) and s["export_credit"] == pytest.approx(1 * 0.05)
    assert s["energy_cost"] == pytest.approx(0.2 - 0.05)


def test_weekday_weekend_and_seasonal_periods():
    t = {"name": "season", "simulation_day": {"month": 7, "day_type": "weekend"}, "periods": [
        {"label": "winter wd", "start": 0, "end": 24, "price": 0.30, "days": "weekday", "months": [1, 2, 3, 10, 11, 12]},
        {"label": "winter we", "start": 0, "end": 24, "price": 0.20, "days": "weekend", "months": [1, 2, 3, 10, 11, 12]},
        {"label": "summer wd", "start": 0, "end": 24, "price": 0.25, "days": "weekday", "months": [4, 5, 6, 7, 8, 9]},
        {"label": "summer we", "start": 0, "end": 24, "price": 0.10, "days": "weekend", "months": [4, 5, 6, 7, 8, 9]}]}
    assert validate_tariff(t) == []
    assert summarise([(16.0, 4.0)], t)["energy_charge"] == pytest.approx(0.1)  # simulation_day = July weekend
    gap = {**t, "periods": t["periods"][:3]}
    assert any("weekend" in i and "gaps" in i for i in validate_tariff(gap))
    sat, mon = datetime(2026, 1, 3, 12), datetime(2026, 7, 6, 12)
    b = bill(t, [(sat, 1.0, 4.0), (mon, 1.0, 4.0)])
    assert b["energy_charge"] == pytest.approx(0.20 + 0.25) and b["days"] == 2
    assert validate_tariff({**FLAT, "periods": [{**FLAT["periods"][0], "days": "sunday"}]})


def test_demand_charge_bases():
    day = {**FLAT, "demand_charge_per_kw": 1.0}
    month = {**day, "demand_basis": "billing_period", "billing_days": 30}
    readings = [(16.0, 2.0), (16.25, 6.0)]
    assert summarise(readings, day)["demand_charge"] == pytest.approx(6.0)
    assert summarise(readings, month)["demand_charge"] == pytest.approx(6.0 / 30)  # prorated for a one-day estimate
    d0 = datetime(2026, 3, 1)
    series = [(d0, 1.0, 2.0), (d0 + timedelta(days=1), 1.0, 5.0)]
    assert bill(day, series)["demand_charge"] == pytest.approx(2.0 + 5.0)  # each day's peak
    assert bill(month, series)["demand_charge"] == pytest.approx(5.0)  # one billing-period peak


def test_tariff_api_persists_extensions_in_the_database(client, db_path):
    body = {"name": "Generic example", "demand_charge_per_kw": 0.0, "fixed_daily_charge": 0.4, "tax_pct": 5,
            "periods": [{"label": "all", "start": 0, "end": 24, "price": 0.2}]}
    r = client.post("/api/tariffs", json=body)
    assert r.status_code == 201, r.text
    t = r.json()
    assert t["fixed_daily_charge"] == 0.4 and t["tax_pct"] == 5
    assert client.put(f"/api/tariffs/{t['id']}", json={**body, "tax_pct": 7}).json()["tax_pct"] == 7
    from fastapi.testclient import TestClient

    from wattwise.api.app import create_app

    with TestClient(create_app(db_path)) as c2:
        got = next(x for x in c2.get("/api/tariffs").json() if x["id"] == t["id"])
        assert got["tax_pct"] == 7
    assert client.post("/api/tariffs", json={**body, "name": "bad", "tax_pct": 200}).status_code == 422
    from pathlib import Path

    assert not list(Path(db_path).parent.glob("tariffs.json"))
