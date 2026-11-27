"""Pure-Python tests of the household model and tariff library (no SDK, no HTTP)."""

from __future__ import annotations

import pytest
from inputs import SRC, TEST_APPLIANCES, TEST_EV, TEST_SETUP, TEST_TARIFFS

from wattwise.adapters.household_model import MissingInput, clock, departure_abs, price_at, simulate, summarise, validate_tariff
from wattwise.services.tariffs import LEGACY_EXAMPLE_TARIFFS, TariffError, TariffStore

TOU = TEST_TARIFFS[0]
FLAT = TEST_TARIFFS[1]
DEMAND = TEST_TARIFFS[2]


def household(tariff=TOU, **ev):
    return {"ev": {**TEST_EV, **ev}, "tariff": tariff, "base_load_kw": 0.35, "appliances": [dict(a) for a in TEST_APPLIANCES]}


# ------------------------------------------------------------------ tariffs
def test_prices_wrap_past_midnight():
    assert price_at(TOU, 23.5) == 0.11 and price_at(TOU, 24.0) == 0.11 and price_at(TOU, 30.0) == 0.11  # 06:00 next day is still night
    assert price_at(TOU, 31.0) == 0.22  # 07:00 next day = day rate
    assert price_at(TOU, 17.0) == 0.36 and price_at(TOU, 21.5) == 0.22


def test_default_tariffs_are_valid():
    assert all(validate_tariff(t) == [] for t in TEST_TARIFFS)


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
    m = simulate(TEST_SETUP, household(FLAT))["metrics"]
    assert m["energy_cost"] == pytest.approx(m["energy_kwh"] * 0.25) and m["demand_charge"] == 0


def test_cost_by_period_adds_up_to_the_energy_charge():
    m = simulate(TEST_SETUP, household())["metrics"]
    assert sum(p["cost"] for p in m["cost_by_period"].values()) == pytest.approx(m["energy_charge"])
    assert sum(p["kwh"] for p in m["cost_by_period"].values()) == pytest.approx(m["energy_kwh"])


def test_demand_charge_scales_with_peak():
    m = simulate(TEST_SETUP, household(DEMAND))["metrics"]
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
    assert late["departure_charge_pct"] < 40 and slow["departure_charge_pct"] < TEST_EV["desired_charge_pct"]


def test_charging_tapers_above_80_percent():
    steps = simulate({"charge_start": 19.0, "charge_power_kw": 11.0}, household(initial_soc_pct=70.0))["steps"]
    powers = [s["ev"] for s in steps if s["ev"] > 0]
    assert max(powers) == pytest.approx(11.0) and powers[-2] < powers[0]  # power falls as the battery fills


def test_moving_flexible_appliances_off_peak_lowers_cost_but_fixed_ones_stay():
    hh = household()
    peak = simulate({**TEST_SETUP, "appliance_start": 19.0, "charge_start": 31.0}, hh)
    night = simulate({**TEST_SETUP, "appliance_start": 24.0, "charge_start": 31.0}, hh)
    assert night["metrics"]["energy_cost"] < peak["metrics"]["energy_cost"]
    for sim in (peak, night):  # the non-flexible tumble dryer (fixed 18:00) always runs
        assert sim["steps"][int((18.0 - 16.0) / 0.25)]["appliances"] == pytest.approx(2.0)


def test_inflexible_appliance_ignores_the_schedule_and_energy_is_conserved():
    hh = household()
    a = simulate({**TEST_SETUP, "appliance_start": 20.0}, hh)["metrics"]["energy_kwh"]
    b = simulate({**TEST_SETUP, "appliance_start": 26.0}, hh)["metrics"]["energy_kwh"]
    assert a == pytest.approx(b)  # same appliances, only shifted in time


def test_projection_is_deterministic_and_adds_no_noise():
    hh = household()
    a, b = simulate(TEST_SETUP, hh), simulate(TEST_SETUP, hh)
    assert a["metrics"] == b["metrics"] and a["steps"] == b["steps"]
def test_background_load_is_the_owner_value_with_no_invented_peaks():
    hh = {**household(), "ev": None, "appliances": []}
    steps = simulate({}, hh)["steps"]
    assert {s["base"] for s in steps} == {0.35} and {s["kw"] for s in steps} == {0.35}  # flat: exactly what the owner entered
def test_missing_inputs_are_reported_not_invented():
    with pytest.raises(MissingInput, match="background load"):
        simulate({}, {**household(), "base_load_kw": None})
    with pytest.raises(MissingInput, match="tariff"):
        simulate({}, {**household(), "tariff": None})
    with pytest.raises(MissingInput, match="plug-in time"):
        simulate({}, household())  # the EV has no charger capacity and no schedule given
def test_current_schedule_comes_from_the_households_own_times():
    from wattwise.adapters.household_model import baseline_setup, with_baseline
    hh = {**household(), "charger_capacity_kw": 3.3}
    assert baseline_setup(hh) == {"charge_start": 19.0, "charge_power_kw": 3.3, "appliance_start__dishwasher": 20.0, "appliance_start__washing_machine": 19.0}
    shared = with_baseline(hh, {"appliance_start": 22.0})
    assert shared == {"charge_start": 19.0, "charge_power_kw": 3.3, "appliance_start": 22.0}


def test_window_covers_exactly_24_hours_in_quarter_hours():
    steps = simulate(TEST_SETUP, household())["steps"]
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


def _seed(s: TariffStore) -> None:
    for t in TEST_TARIFFS:
        s.save({**{k: v for k, v in t.items() if k != "id"}, "source": SRC}, None)
def test_store_starts_empty_and_persists(tmp_path):
    p = _db(tmp_path)
    assert TariffStore(p).list() == []  # nothing is seeded
    s = TariffStore(p)
    saved = s.save({"name": "My Tariff", "source": SRC, "demand_charge_per_kw": 0.1, "periods": [{"label": "all", "start": 0, "end": 24, "price": 0.3}]})
    assert saved["id"] == "my-tariff" and TariffStore(p).get("my-tariff")["periods"][0]["price"] == 0.3
    assert TariffStore(p).get("my-tariff")["source"]["source_name"] == SRC["source_name"]
    assert not list(tmp_path.glob("*.json"))  # no tariffs.json any more
def test_stored_example_tariffs_from_earlier_versions_are_labelled_and_not_usable(tmp_path):
    import json
    from datetime import UTC, datetime

    from db.models.wattwise import Tariff
    p = _db(tmp_path)
    now = datetime.now(UTC)
    with p.begin() as c:
        for i, (tid, periods) in enumerate(LEGACY_EXAMPLE_TARIFFS.items()):
            c.execute(Tariff.__table__.insert().values(tariff_id=tid, name=tid, position=i, created_at=now, updated_at=now,
                                                       data_json=json.dumps({"id": tid, "name": tid, "demand_charge_per_kw": 0.0, "periods": periods})))
        edited = {"id": "flat-rate", "name": "flat-rate", "periods": [{"label": "All day", "start": 0, "end": 24, "price": 0.31}]}
        c.execute(Tariff.__table__.update().where(Tariff.tariff_id == "flat-rate").values(data_json=json.dumps(edited)))
    got = {t["id"]: t for t in TariffStore(p).list()}
    assert got["time-of-use"]["source"]["source_type"] == "example" and got["time-of-use"]["source_status"]["usable"] is False
    assert got["flat-rate"]["source"]["source_type"] == "unverified" and got["flat-rate"]["source_status"]["usable"] is True


def test_store_rejects_invalid_duplicate_and_unknown(tmp_path):
    s = TariffStore(_db(tmp_path))
    _seed(s)
    with pytest.raises(TariffError):
        s.save({"name": "bad", "periods": [{"label": "x", "start": 0, "end": 5, "price": 0.1}]})
    with pytest.raises(TariffError):
        s.save({"name": "Flat Rate", "periods": FLAT["periods"]})  # same name as a stored tariff
    with pytest.raises(KeyError):
        s.get("nope")
    with pytest.raises(KeyError):
        s.save({"name": "x", "periods": FLAT["periods"]}, tariff_id="nope")


def test_store_update_and_delete_down_to_an_empty_library(tmp_path):
    s = TariffStore(_db(tmp_path))
    _seed(s)
    s.save({"name": "Flat rate", "source": SRC, "periods": [{"label": "all", "start": 0, "end": 24, "price": 0.4}]}, tariff_id="flat-rate")
    assert s.get("flat-rate")["periods"][0]["price"] == 0.4
    s.delete("time-of-use")
    s.delete("night-saver-demand")
    s.delete("flat-rate")  # the last tariff can go: an empty library is valid
    assert s.list() == []
    s.save({"name": "Extra", "source": SRC, "periods": FLAT["periods"]})
    with pytest.raises(KeyError):
        s.delete("time-of-use")  # already deleted
