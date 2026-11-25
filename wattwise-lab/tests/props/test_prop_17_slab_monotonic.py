"""Property 17: slab cost is monotonic in billing-cycle consumption.

**Validates: Requirements 7.8**
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from wattwise.adapters import household_model as HM

STEP = HM.STEP_H


@st.composite
def nondecreasing_tariffs(draw):
    """Billing-period slab tariffs whose tier adders do not decrease with higher tiers (C5)."""
    n = draw(st.integers(min_value=1, max_value=4))
    limits = sorted(draw(st.lists(st.integers(1, 400), min_size=n - 1, max_size=n - 1, unique=True)))
    adders = sorted(draw(st.lists(st.floats(0, 2, allow_nan=False), min_size=n, max_size=n)))
    tiers = [{"up_to_kwh": float(u), "adder_per_kwh": a} for u, a in zip(limits, adders)]
    tiers.append({"up_to_kwh": None, "adder_per_kwh": adders[-1]})
    return {"id": "t", "name": "t", "demand_charge_per_kw": 0.0, "tier_basis": "billing_period", "tiers": tiers,
            "periods": [{"label": "All", "start": 0, "end": 24, "price": draw(st.floats(0, 1, allow_nan=False))}]}  # fmt: skip


profiles = st.lists(st.floats(0, 8, allow_nan=False), min_size=1, max_size=96)
consumption = st.floats(0, 1000, allow_nan=False).map(lambda v: round(v, 3))


# Feature: wattwise-household-model-improvements, Property 17: Slab cost is monotonic in consumption
@settings(max_examples=100, deadline=None)
@given(tariff=nondecreasing_tariffs(), kws=profiles, x=consumption, y=consumption)
def test_slab_cost_monotonic_in_consumption(tariff, kws, x, y):
    a, b = sorted((x, y))
    readings = [(HM.WINDOW_START + i * STEP, kw) for i, kw in enumerate(kws)]
    cost_a = HM.summarise(readings, tariff, billing_cycle_kwh=a)["energy_cost"]
    cost_b = HM.summarise(readings, tariff, billing_cycle_kwh=b)["energy_cost"]
    assert cost_b >= cost_a - 1e-9
