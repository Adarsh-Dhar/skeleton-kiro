"""Pure-Python tests of the household model and tariff library (no SDK, no HTTP)."""

from __future__ import annotations

import pytest

from wattwise.adapters.household_model import (
    BASELINE,
    DEFAULT_APPLIANCES,
    DEFAULT_EV,
    DEFAULT_TARIFFS,
    clock,
    departure_abs,
    price_at,
    rng_for,
    simulate,
    summarise,
    validate_tariff,
)
from wattwise.services.tariffs import TariffError, TariffStore

TOU = DEFAULT_TARIFFS[0]
FLAT = DEFAULT_TARIFFS[1]
DEMAND = DEFAULT_TARIFFS[2]


def household(tariff=TOU, **ev):
    return {"ev": {**DEFAULT_EV, **ev}, "tariff": tariff, "base_load_kw": 0.35, "appliances": [dict(a) for a in DEFAULT_APPLIANCES]}


# ------------------------------------------------------------------ tariffs
def test_prices_wrap_past_midnight():
    assert price_at(TOU, 23.5) == 0.11 and price_at(TOU, 24.0) == 0.11 and price_at(TOU, 30.0) == 0.11  # 06:00 next day is still night
    assert price_at(TOU, 31.0) == 0.22  # 07:00 next day = day rate
    assert price_at(TOU, 17.0) == 0.36 and price_at(TOU, 21.5) == 0.22


def test_default_tariffs_are_valid():
    assert all(validate_tariff(t) == [] for t in DEFAULT_TARIFFS)


@pytest.mark.parametrize(
    "periods,fragment",
    [
        ([{"label": "a", "start": 0, "end": 12, "price": 0.1}], "gaps"),
        ([{"label": "a", "start": 0, "end": 24, "price": 0.1}, {"label": "b", "start": 6, "end": 8, "price": 0.2}], "overlap"),
        ([{"label": "a", "start": 0, "end": 24, "price": -1}], "price must be between"),
        ([{"label": "a", "start": 5, "end": 5, "price": 0.1}], "differ"),
        ([], "at least one price period"),
    ],
)
def test_invalid_tariffs_are_explained(periods, fragment):
    issues = validate_tariff({"name": "x", "periods": periods})
    assert any(fragment in i for i in issues), issues


def test_blank_tariff_name_and_excess_demand_charge_are_rejected():
    assert any("name" in i for i in validate_tariff({"name": " ", "periods": FLAT["periods"]}))
    assert any("demand charge" in i for i in validate_tariff({"name": "x", "periods": FLAT["periods"], "demand_charge_per_kw": 500}))


def test_clock_formatting():
    assert clock(19.0) == "19:00" and clock(31.5) == "07:30+1d" and clock(24.0) == "00:00+1d"
    assert departure_abs(7.5) == 31.5 and departure_abs(17.0) == 17.0


# ------------------------------------------------------------------ simulation
def test_flat_tariff_cost_is_energy_times_price():
    m = simulate(BASELINE, household(FLAT))["metrics"]
    assert m["energy_cost"] == pytest.approx(m["energy_kwh"] * 0.25) and m["demand_charge"] == 0


def test_cost_by_period_adds_up_to_the_energy_charge():
    m = simulate(BASELINE, household())["metrics"]
    assert sum(p["cost"] for p in m["cost_by_period"].values()) == pytest.approx(m["energy_charge"])
    assert sum(p["kwh"] for p in m["cost_by_period"].values()) == pytest.approx(m["energy_kwh"])


def test_demand_charge_scales_with_peak():
    m = simulate(BASELINE, household(DEMAND))["metrics"]
    assert m["demand_charge"] == pytest.approx(0.60 * m["peak_power_kw"]) and m["energy_cost"] == pytest.approx(m["energy_charge"] + m["demand_charge"])


def test_ev_charging_reaches_a_full_battery_when_given_time_and_power():
    m = simulate({"charge_start": 19.0, "charge_power_kw": 11.0}, household())["metrics"]
    assert m["departure_charge_pct"] == pytest.approx(100.0, abs=0.5)
    # energy drawn from the grid exceeds energy stored because of charging losses
    stored = 60.0 * 0.70
    assert m["ev_energy_kwh"] == pytest.approx(stored / 0.92, rel=0.02)


def test_late_start_or_slow_charger_leaves_the_car_short():
    late = simulate({"charge_start": 31.0, "charge_power_kw": 7.4}, household())["metrics"]
    slow = simulate({"charge_start": 23.0, "charge_power_kw": 1.4}, household())["metrics"]
    assert late["departure_charge_pct"] < 40 and slow["departure_charge_pct"] < DEFAULT_EV["desired_charge_pct"]


def test_charging_tapers_above_80_percent():
    steps = simulate({"charge_start": 19.0, "charge_power_kw": 11.0}, household(initial_soc_pct=70.0))["steps"]
    powers = [s["ev"] for s in steps if s["ev"] > 0]
    assert max(powers) == pytest.approx(11.0) and powers[-2] < powers[0]  # power falls as the battery fills


def test_moving_flexible_appliances_off_peak_lowers_cost_but_fixed_ones_stay():
    hh = household()
    peak = simulate({**BASELINE, "appliance_start": 19.0, "charge_start": 31.0}, hh)
    night = simulate({**BASELINE, "appliance_start": 24.0, "charge_start": 31.0}, hh)
    assert night["metrics"]["energy_cost"] < peak["metrics"]["energy_cost"]
    for sim in (peak, night):  # the non-flexible tumble dryer (fixed 18:00) always runs
        assert sim["steps"][int((18.0 - 16.0) / 0.25)]["appliances"] == pytest.approx(2.0)


def test_inflexible_appliance_ignores_the_schedule_and_energy_is_conserved():
    hh = household()
    a = simulate({**BASELINE, "appliance_start": 20.0}, hh)["metrics"]["energy_kwh"]
    b = simulate({**BASELINE, "appliance_start": 26.0}, hh)["metrics"]["energy_kwh"]
    assert a == pytest.approx(b)  # same appliances, only shifted in time


def test_noise_only_with_rng_and_is_reproducible():
    hh = household()
    clean1, clean2 = simulate(BASELINE, hh), simulate(BASELINE, hh)
    n1, n2 = simulate(BASELINE, hh, rng_for("run-1", "meter")), simulate(BASELINE, hh, rng_for("run-1", "meter"))
    other = simulate(BASELINE, hh, rng_for("run-2", "meter"))
    assert clean1["metrics"] == clean2["metrics"] and n1["metrics"] == n2["metrics"]
    assert n1["metrics"]["energy_kwh"] != clean1["metrics"]["energy_kwh"] and other["metrics"] != n1["metrics"]


def test_window_covers_exactly_24_hours_in_quarter_hours():
    steps = simulate(BASELINE, household())["steps"]
    assert len(steps) == 96 and steps[0]["t"] == 16.0 and steps[-1]["t"] == 39.75


def test_summarise_handles_a_custom_interval():
    s = summarise([(0.0, 2.0), (1.0, 4.0)], FLAT, step_h=1.0)
    assert s["energy_kwh"] == pytest.approx(6.0) and s["peak_power_kw"] == 4.0 and s["energy_cost"] == pytest.approx(1.5)


# ------------------------------------------------------------------ tariff store (root database)
def _db(tmp_path):
    from db import create_db_engine, migrate

    e = create_db_engine(tmp_path / "trialforge.db")
    migrate(e)
    return e


def test_store_seeds_defaults_and_persists(tmp_path):
    p = _db(tmp_path)
    assert {t["id"] for t in TariffStore(p).list()} == {"time-of-use", "flat-rate", "night-saver-demand"}
    s = TariffStore(p)
    saved = s.save({"name": "My Tariff", "demand_charge_per_kw": 0.1, "periods": [{"label": "all", "start": 0, "end": 24, "price": 0.3}]})
    assert saved["id"] == "my-tariff" and TariffStore(p).get("my-tariff")["periods"][0]["price"] == 0.3
    assert not list(tmp_path.glob("*.json"))  # no tariffs.json any more


def test_store_rejects_invalid_duplicate_and_unknown(tmp_path):
    s = TariffStore(_db(tmp_path))
    with pytest.raises(TariffError):
        s.save({"name": "bad", "periods": [{"label": "x", "start": 0, "end": 5, "price": 0.1}]})
    with pytest.raises(TariffError):
        s.save({"name": "Flat Rate", "periods": FLAT["periods"]})  # same name as a stored tariff
    with pytest.raises(KeyError):
        s.get("nope")
    with pytest.raises(KeyError):
        s.save({"name": "x", "periods": FLAT["periods"]}, tariff_id="nope")


def test_store_update_and_delete_keep_at_least_one(tmp_path):
    s = TariffStore(_db(tmp_path))
    s.save({"name": "Flat rate", "periods": [{"label": "all", "start": 0, "end": 24, "price": 0.4}]}, tariff_id="flat-rate")
    assert s.get("flat-rate")["periods"][0]["price"] == 0.4
    s.delete("time-of-use")
    s.delete("night-saver-demand")
    with pytest.raises(TariffError):
        s.delete("flat-rate")  # the last tariff cannot be deleted
    s.save({"name": "Extra", "periods": FLAT["periods"]})
    with pytest.raises(KeyError):
        s.delete("time-of-use")  # already deleted
