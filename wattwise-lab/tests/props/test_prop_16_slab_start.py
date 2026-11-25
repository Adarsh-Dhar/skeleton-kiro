"""Property 16: slab accumulation starts at the supplied billing-cycle consumption.

**Validates: Requirements 7.2, 7.3, 7.5**
"""

from __future__ import annotations

import math

from hypothesis import given, settings
from hypothesis import strategies as st

from wattwise.adapters import household_model as HM

STEP = HM.STEP_H


@st.composite
def tiered_tariffs(draw):
    n = draw(st.integers(min_value=1, max_value=4))
    limits = sorted(draw(st.lists(st.integers(1, 400), min_size=n - 1, max_size=n - 1, unique=True)))
    adders = draw(st.lists(st.floats(0, 2, allow_nan=False), min_size=n, max_size=n))
    tiers = [{"up_to_kwh": float(u), "adder_per_kwh": a} for u, a in zip(limits, adders)]
    tiers.append({"up_to_kwh": None, "adder_per_kwh": adders[-1]})
    tiers = tiers[:n]
    tiers[-1] = {"up_to_kwh": None, "adder_per_kwh": adders[n - 1]}
    return {"id": "t", "name": "t", "demand_charge_per_kw": 0.0, "tier_basis": "billing_period", "tiers": tiers,
            "periods": [{"label": "All", "start": 0, "end": 24, "price": draw(st.floats(0, 1, allow_nan=False))}]}


profiles = st.lists(st.floats(0, 8, allow_nan=False), min_size=1, max_size=96)
consumption = st.floats(0, 1000, allow_nan=False).map(lambda v: round(v, 3))


def _readings(kws):
    return [(HM.WINDOW_START + i * STEP, kw) for i, kw in enumerate(kws)]


def _slab_cost(tariff, start, kwh):
    """Independent calc: integral of (price + adder) over [start, start + kwh] along the tier boundaries."""
    price = tariff["periods"][0]["price"]
    total, pos, left = 0.0, start, kwh
    for tier in tariff["tiers"]:
        up = tier["up_to_kwh"]
        if up is not None and pos >= up:
            continue
        room = left if up is None else min(left, up - pos)
        total += room * (price + tier["adder_per_kwh"])
        pos += room
        left -= room
        if left <= 1e-12:
            break
    return total


# Feature: wattwise-household-model-improvements, Property 16: Slab accumulation starts at the supplied value
@settings(max_examples=100, deadline=None)
@given(tariff=tiered_tariffs(), kws=profiles, c=consumption)
def test_slab_starts_at_supplied_value(tariff, kws, c):
    readings = _readings(kws)
    kwh = sum(kw * STEP for kw in kws)
    got = HM.summarise(readings, tariff, billing_cycle_kwh=c)["energy_cost"]
    assert math.isclose(got, _slab_cost(tariff, c, kwh), rel_tol=1e-9, abs_tol=1e-9)
    # Omitting c is the same as c = 0
    omitted = HM.summarise(readings, tariff)["energy_cost"]
    zero = HM.summarise(readings, tariff, billing_cycle_kwh=0.0)["energy_cost"]
    assert math.isclose(omitted, zero, rel_tol=1e-12, abs_tol=1e-12)
    assert math.isclose(zero, _slab_cost(tariff, 0.0, kwh), rel_tol=1e-9, abs_tol=1e-9)


# Feature: wattwise-household-model-improvements, Property 16: Slab accumulation starts at the supplied value
@settings(max_examples=100, deadline=None)
@given(
    kws=profiles,
    c1=consumption,
    c2=consumption,
    price=st.floats(0, 1, allow_nan=False),
    surcharge=st.floats(0, 0.5, allow_nan=False),
    daily_tiers=st.booleans(),
)
def test_cost_without_billing_period_tiers_ignores_value(kws, c1, c2, price, surcharge, daily_tiers):
    tariff = {"id": "n", "name": "n", "demand_charge_per_kw": 0.0, "surcharge_per_kwh": surcharge,  # extended, but no tiers
              "periods": [{"label": "All", "start": 0, "end": 24, "price": price}]}  # fmt: skip
    if daily_tiers:  # tiers with daily basis do not use the billing-cycle value either
        tariff["tiers"] = [{"up_to_kwh": 5.0, "adder_per_kwh": 0.1}, {"up_to_kwh": None, "adder_per_kwh": 0.3}]
        tariff["tier_basis"] = "daily"
    readings = _readings(kws)
    a = HM.summarise(readings, tariff, billing_cycle_kwh=c1)["energy_cost"]
    b = HM.summarise(readings, tariff, billing_cycle_kwh=c2)["energy_cost"]
    assert math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-12)
