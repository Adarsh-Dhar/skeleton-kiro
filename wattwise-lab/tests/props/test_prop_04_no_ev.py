"""Property 4: a no-EV simulation has no EV load.

**Validates: Requirements 2.2, 2.3, 2.6**
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
from inputs import TEST_TARIFFS

from wattwise.adapters import household_model as HM

appliance = st.fixed_dictionaries({
    "name": st.sampled_from(["Dishwasher", "Washer", "Dryer", "Oven"]),
    "power_kw": st.integers(1, 40).map(lambda v: v / 10),
    "duration_h": st.integers(1, 12).map(lambda v: v / 4),
    "flexible": st.booleans(),
    "fixed_start": st.integers(16 * 4, 38 * 4).map(lambda v: v / 4),
})
setups = st.fixed_dictionaries({
    "charge_start": st.floats(17.0, 30.0),
    "charge_power_kw": st.floats(1.4, 11.0),
    "appliance_start": st.floats(17.0, 30.0),
})


# Feature: wattwise-household-model-improvements, Property 4: No-EV simulation has no EV load
@settings(max_examples=100, deadline=None)
@given(
    apps=st.lists(appliance, min_size=1, max_size=4).filter(lambda a: any(x["flexible"] for x in a)),
    setup=setups,
    base=st.integers(0, 100).map(lambda v: v / 100),
    tariff=st.sampled_from(TEST_TARIFFS),
)
def test_no_ev_simulation_has_no_ev_load(apps, setup, base, tariff):
    hh = {"ev": None, "tariff": tariff, "base_load_kw": base, "appliances": apps}
    sim = HM.simulate(setup, hh)
    steps, m = sim["steps"], sim["metrics"]

    assert all(s["ev"] == 0.0 for s in steps)
    assert m["ev_energy_kwh"] == 0.0
    assert "departure_charge_pct" not in m

    expected = sum(s["base"] + s["appliances"] for s in steps) * HM.STEP_H
    assert abs(m["energy_kwh"] - expected) <= 0.001
    assert abs(sum(s["kw"] for s in steps) * HM.STEP_H - expected) <= 0.001

    limits = HM.factor_limits(None, apps, False)
    assert "charge_start" not in limits and "charge_power_kw" not in limits
    assert "appliance_start" in limits
