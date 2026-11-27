"""Property 10: changing one appliance's start time leaves every other appliance's load profile unchanged.

**Validates: Requirements 4.5, 4.9**
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
from inputs import TEST_EV, TEST_TARIFFS

from wattwise.adapters import household_model as HM

starts = st.floats(17.0, 30.0, allow_nan=False, allow_infinity=False)


@st.composite
def scenarios(draw):
    n = draw(st.integers(2, 6))
    apps = [{"name": f"Appliance {i}", "id": f"a{i}", "power_kw": draw(st.floats(0.1, 4.0, allow_nan=False)),
             "duration_h": draw(st.floats(0.25, 5.0, allow_nan=False)), "flexible": True, "fixed_start": 18.0} for i in range(n)]
    if draw(st.booleans()):
        apps.append({"name": "Fixed", "id": "fixed", "power_kw": 1.0, "duration_h": 1.0, "flexible": False,
                     "fixed_start": draw(starts)})
    setup = {HM.appliance_key(a): draw(starts) for a in apps if a["flexible"]}
    flexible = [a for a in apps if a["flexible"]]
    changed = draw(st.sampled_from(flexible))
    new_start = draw(starts)
    household = {"ev": TEST_EV, "tariff": TEST_TARIFFS[1], "appliances": apps, "base_load_kw": 0.35, "charger_capacity_kw": 11.0}
    return household, setup, changed, new_start


# Feature: wattwise-household-model-improvements, Property 10: Appliance independence
@settings(max_examples=100, deadline=None)
@given(scenarios())
def test_changing_one_appliance_start_leaves_others_unchanged(scenario):
    household, setup, changed, new_start = scenario
    key = HM.appliance_key(changed)
    changed_id = HM.appliance_id(changed["name"], changed.get("id"))
    before = HM.simulate(setup, household)["steps"]
    after = HM.simulate({**setup, key: new_start}, household)["steps"]
    assert len(before) == len(after)
    for b, a in zip(before, after):
        assert b["by_appliance"].keys() == a["by_appliance"].keys()
        for aid, kw in b["by_appliance"].items():
            if aid != changed_id:
                assert abs(kw - a["by_appliance"][aid]) <= 1e-9
