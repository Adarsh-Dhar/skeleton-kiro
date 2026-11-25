"""Property 3: over-capacity charging power is rejected.

**Validates: Requirements 1.5**
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from wattwise.adapters import household_model as HM

TARIFF = HM.DEFAULT_TARIFFS[0]
capacities = st.integers(140, 35000).map(lambda c: c / 100)  # 1.40 to 350.00 kW
powers = st.integers(140, 35000).map(lambda c: c / 100)


def _household(capacity: float) -> dict:
    return {
        "ev": dict(HM.DEFAULT_EV),
        "tariff": TARIFF,
        "base_load_kw": 0.35,
        "appliances": [dict(a) for a in HM.DEFAULT_APPLIANCES],
        "charger_capacity_kw": capacity,
    }


# Feature: wattwise-household-model-improvements, Property 3: Over-capacity charging power is rejected
@settings(max_examples=100, deadline=None)
@given(capacity=capacities, power=powers)
def test_over_capacity_rejected_else_result(capacity, power):
    household = _household(capacity)
    setup = {**HM.BASELINE, "charge_power_kw": power}
    if power > capacity:
        with pytest.raises(HM.ChargerCapacityError) as exc:
            HM.simulate(setup, household)
        assert f"{power:g}" in str(exc.value) and f"{capacity:g}" in str(exc.value)
    else:
        result = HM.simulate(setup, household)
        assert len(result["steps"]) == 96 and "metrics" in result
