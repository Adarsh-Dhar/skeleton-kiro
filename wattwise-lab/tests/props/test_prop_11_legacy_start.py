"""Property 11: legacy start factor expansion and conflict.

**Validates: Requirements 4.7, 4.8**
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from wattwise.adapters import household_model as HM


@st.composite
def households(draw):
    n = draw(st.integers(1, 6))
    apps = []
    for i in range(n):
        apps.append({
            "id": f"app-{i}", "name": f"Appliance {i}",
            "power_kw": draw(st.floats(0.1, 3.0, allow_nan=False)),
            "duration_h": draw(st.floats(0.25, 4.0, allow_nan=False)),
            "flexible": draw(st.booleans()) if i else True,  # at least one flexible appliance
            "fixed_start": draw(st.floats(17, 30, allow_nan=False)),
        })
    ev = draw(st.one_of(st.none(), st.just(dict(HM.DEFAULT_EV))))
    return {"ev": ev, "tariff": HM.DEFAULT_TARIFFS[0], "appliances": apps}


start_times = st.floats(17.0, 30.0, allow_nan=False)


def _flexible(h):
    return [a for a in h["appliances"] if a["flexible"]]


# Feature: wattwise-household-model-improvements, Property 11: Legacy start factor expansion and conflict
@settings(max_examples=100, deadline=None)
@given(h=households(), v=start_times)
def test_legacy_start_equals_per_appliance_starts(h, v):
    legacy = HM.simulate({"appliance_start": v}, h)
    expanded = HM.simulate({HM.appliance_key(a): v for a in _flexible(h)}, h)
    assert legacy["steps"] == expanded["steps"]
    assert legacy["metrics"] == expanded["metrics"]


# Feature: wattwise-household-model-improvements, Property 11: Legacy start factor expansion and conflict
@settings(max_examples=100, deadline=None)
@given(h=households(), v=start_times, data=st.data())
def test_mixing_legacy_and_per_appliance_raises_conflict(h, v, data):
    flex = _flexible(h)
    chosen = data.draw(st.lists(st.sampled_from(flex), min_size=1, max_size=len(flex), unique_by=HM.appliance_key))
    setup = {"appliance_start": v, **{HM.appliance_key(a): data.draw(start_times) for a in chosen}}
    with pytest.raises(HM.FactorConflict) as exc:
        HM.simulate(setup, h)
    with pytest.raises(HM.FactorConflict):
        HM.check_setup_keys(setup)
    assert "appliance_start" in str(exc.value)
    for a in chosen:
        assert HM.appliance_key(a) in str(exc.value)
