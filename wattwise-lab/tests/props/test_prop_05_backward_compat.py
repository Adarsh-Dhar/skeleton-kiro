"""Property 5: backward compatibility with the previous model.

Old-format inputs (EV present, no charger capacity, shared appliance start, no window date, no billing cycle value, uniform
meter intervals) give results equal to the frozen copy of the previous implementation. New additive keys (``by_appliance``,
new metric keys) are ignored; every legacy key must match exactly.

**Validates: Requirements 2.5, 4.6, 6.6, 7.3, 7.5, 8.5, 9.8, 16.6, 16.7**
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

import legacy_reference as LHM
import legacy_tariff_reference as LTM
from wattwise.adapters import household_model as HM
from wattwise.adapters import tariff_model as TM


def assert_legacy_subset(expected, actual, path="result"):
    """Every key/value in ``expected`` is present and equal in ``actual``; extra keys in ``actual`` are allowed."""
    if isinstance(expected, dict):
        assert isinstance(actual, dict), f"{path}: expected dict, got {type(actual)}"
        for k, v in expected.items():
            assert k in actual, f"{path}: missing key {k!r}"
            assert_legacy_subset(v, actual[k], f"{path}.{k}")
    elif isinstance(expected, (list, tuple)):
        assert len(expected) == len(actual), f"{path}: length {len(expected)} != {len(actual)}"
        for i, (e, a) in enumerate(zip(expected, actual)):
            assert_legacy_subset(e, a, f"{path}[{i}]")
    else:
        assert expected == actual, f"{path}: legacy {expected!r} != new {actual!r}"


# ---------- generators ----------
def _tiling(prices, cuts):
    """Periods tiling [0, 24) at the sorted distinct cut hours (first period starts at 0, last ends at 24)."""
    bounds = [0, *sorted(set(cuts)), 24]
    return [{"label": f"P{i}", "start": s, "end": e, "price": prices[i % len(prices)]}
            for i, (s, e) in enumerate(zip(bounds, bounds[1:]))]


price = st.integers(0, 80).map(lambda v: v / 100)
tilings = st.builds(_tiling, st.lists(price, min_size=1, max_size=6), st.lists(st.integers(1, 23), max_size=5))


@st.composite
def tariffs(draw):
    t = {"name": "T", "demand_charge_per_kw": draw(st.sampled_from([0.0, 0.0, 0.6, 1.25])), "periods": draw(tilings)}
    kind = draw(st.sampled_from(["plain", "extended", "seasonal", "daytype"]))
    if kind == "seasonal":  # months partition the year; one tiling per half
        t["periods"] = [{**p, "months": list(range(1, 7))} for p in t["periods"]] + \
                       [{**p, "months": list(range(7, 13))} for p in draw(tilings)]
        t["simulation_day"] = {"month": draw(st.integers(1, 12)), "day_type": "weekday"}
    elif kind == "daytype":
        t["periods"] = [{**p, "days": "weekday"} for p in t["periods"]] + [{**p, "days": "weekend"} for p in draw(tilings)]
        t["simulation_day"] = {"month": draw(st.integers(1, 12)), "day_type": draw(st.sampled_from(["weekday", "weekend"]))}
    if kind != "plain":
        if draw(st.booleans()):
            t["fixed_daily_charge"] = draw(st.integers(0, 200)) / 100
        if draw(st.booleans()):
            t["fixed_monthly_charge"] = draw(st.integers(0, 3000)) / 100
        if draw(st.booleans()):
            t["surcharge_per_kwh"] = draw(st.integers(0, 30)) / 100
        if draw(st.booleans()):
            t["tax_pct"] = draw(st.integers(0, 25))
        if draw(st.booleans()):
            t["export_credit_per_kwh"] = draw(st.integers(0, 30)) / 100
        if draw(st.booleans()):
            t["demand_basis"] = draw(st.sampled_from(["daily", "billing_period"]))
        if draw(st.booleans()):
            t["billing_days"] = draw(st.integers(1, 40))
        if draw(st.booleans()):
            ups = sorted(draw(st.sets(st.integers(1, 40), min_size=1, max_size=3)))
            adders = [draw(st.integers(0, 30)) / 100 for _ in range(len(ups) + 1)]
            t["tiers"] = [{"up_to_kwh": float(u), "adder_per_kwh": a} for u, a in zip(ups, adders)] + \
                         [{"up_to_kwh": None, "adder_per_kwh": adders[-1]}]
            t["tier_basis"] = draw(st.sampled_from(["daily", "billing_period"]))
    return t


appliance = st.fixed_dictionaries({
    "name": st.sampled_from(["Dishwasher", "Washer", "Dryer", "Oven"]),
    "power_kw": st.integers(1, 40).map(lambda v: v / 10),
    "duration_h": st.integers(1, 12).map(lambda v: v / 4),
    "flexible": st.booleans(),
    "fixed_start": st.integers(16 * 4, 38 * 4).map(lambda v: v / 4),
})
evs = st.fixed_dictionaries({
    "battery_kwh": st.integers(20, 100).map(float),
    "initial_soc_pct": st.integers(0, 95).map(float),
    "desired_charge_pct": st.integers(50, 100).map(float),
    "departure_hour": st.integers(0, 95).map(lambda v: v / 4),
    "efficiency": st.integers(70, 100).map(lambda v: v / 100),
})
setups = st.fixed_dictionaries({}, optional={
    "charge_start": st.floats(17.0, 31.0),
    "charge_power_kw": st.floats(1.4, 11.0),
    "appliance_start": st.floats(17.0, 30.0),
})


@st.composite
def households(draw):
    hh = {"ev": draw(evs), "tariff": draw(tariffs()), "appliances": draw(st.lists(appliance, max_size=4)),
          "base_load_kw": draw(st.integers(0, 100).map(lambda v: v / 100))}
    if draw(st.booleans()):
        hh["base_profile_kw"] = [draw(st.integers(0, 300)) / 100 for _ in range(96)]
    return hh


# Feature: wattwise-household-model-improvements, Property 5: Backward compatibility with the previous model
@settings(max_examples=100, deadline=None)
@given(hh=households(), setup=setups, seed=st.one_of(st.none(), st.integers(0, 10_000)))
def test_simulate_matches_legacy(hh, setup, seed):
    legacy = LHM.simulate(dict(setup), hh, random.Random(seed) if seed is not None else None)
    new = HM.simulate(dict(setup), hh, random.Random(seed) if seed is not None else None)
    assert_legacy_subset(legacy, new)


# Feature: wattwise-household-model-improvements, Property 5: Backward compatibility with the previous model
@settings(max_examples=100, deadline=None)
@given(
    tariff=tariffs(),
    readings=st.lists(st.tuples(st.integers(0, 95).map(lambda i: 16 + i / 4), st.integers(-300, 1100).map(lambda v: v / 100)), max_size=96),
    with_ev=st.booleans(),
    dep=st.one_of(st.none(), st.integers(0, 100).map(float)),
)
def test_summarise_and_price_at_match_legacy(tariff, readings, with_ev, dep):
    ev_kw = [max(kw, 0.0) for _, kw in readings] if with_ev else None
    assert_legacy_subset(LHM.summarise(readings, tariff, ev_kw=ev_kw, departure_charge_pct=dep),
                         HM.summarise(readings, tariff, ev_kw=ev_kw, departure_charge_pct=dep))
    for t, _ in readings[:8]:
        assert LHM.price_at(tariff, t) == HM.price_at(tariff, t)
        assert LHM.period_label(tariff, t) == HM.period_label(tariff, t)


# Feature: wattwise-household-model-improvements, Property 5: Backward compatibility with the previous model
@settings(max_examples=100, deadline=None)
@given(
    tariff=tariffs(),
    start=st.datetimes(min_value=datetime(2024, 1, 1), max_value=datetime(2025, 12, 1)).map(lambda d: d.replace(minute=0, second=0, microsecond=0)),
    kws=st.lists(st.integers(-200, 900).map(lambda v: v / 100), min_size=1, max_size=96),
)
def test_bill_matches_legacy(tariff, start, kws):
    # uniform hourly meter intervals; the tariff validators of both versions agree that the input is valid
    intervals = [(start + timedelta(hours=i), kw, kw) for i, kw in enumerate(kws)]
    try:
        legacy = LTM.bill(tariff, intervals)
    except ValueError:
        return  # legacy cannot price this input (e.g. no period for a month/day type); not an old-format valid input
    assert_legacy_subset(legacy, TM.bill(tariff, intervals))
