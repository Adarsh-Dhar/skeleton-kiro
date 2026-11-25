"""Property 9: per-appliance start factors exist only in per_appliance mode, one per flexible appliance, with the catalog limits.

**Validates: Requirements 4.1, 4.2, 4.3, 4.4**
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from wattwise.adapters import household_model as HM
from wattwise.api.schemas import ExperimentRequest, FactorSetting
from wattwise.services.lab import LabService

_LAB = LabService(str(Path(tempfile.mkdtemp(prefix="prop09-")) / "lab.db"))
LIMITS = HM.FACTORS["appliance_start"]


@st.composite
def scenarios(draw):
    n_flex = draw(st.integers(1, 8))
    apps = [{"id": f"flex{i}", "name": f"Flexible {i}", "power_kw": 1.0, "duration_h": 1.0, "flexible": True} for i in range(n_flex)]
    apps += [{"id": f"fixed{i}", "name": f"Fixed {i}", "power_kw": 1.0, "duration_h": 1.0, "flexible": False, "fixed_start": 19.0}
             for i in range(draw(st.integers(0, 3)))]
    apps = draw(st.permutations(apps))
    low = draw(st.floats(LIMITS["low"], LIMITS["high"] - 1.0, allow_nan=False))
    high = draw(st.floats(low + 0.5, LIMITS["high"], allow_nan=False))
    mode = draw(st.sampled_from(["shared", "per_appliance"]))
    method = draw(st.sampled_from(["full_factorial", "ofat", "random"]))
    return apps, mode, method, low, high, n_flex


def _build(apps, mode, method, low, high):
    req = ExperimentRequest(name="p9", ev=None, appliances=apps, appliance_start_mode=mode, design={"method": method},
                            factors={"appliance_start": FactorSetting(low=low, high=high)})
    household = _LAB.household(req)
    return _LAB.build_spec(req, "ww-prop09", household), household


# Feature: wattwise-household-model-improvements, Property 9: Appliance start factor generation
@settings(max_examples=100, deadline=None)
@given(scenarios())
def test_start_factor_generation(scenario):
    apps, mode, method, low, high, n_flex = scenario
    spec, household = _build(apps, mode, method, low, high)
    names = [f.name for f in spec.factors]
    assert len(names) == len(set(names))
    if mode == "shared":
        assert names == ["appliance_start"]
        assert (spec.factors[0].lower, spec.factors[0].upper) == (low, high)
        return
    expected = sorted(HM.appliance_key(a) for a in household["appliances"] if a["flexible"])
    assert sorted(names) == expected and len(names) == n_flex
    assert "appliance_start" not in names
    for f in spec.factors:
        assert (f.lower, f.upper) == (low, high)
        assert LIMITS["low"] <= f.lower < f.upper <= LIMITS["high"]
    if n_flex < 2 and method != "ofat":
        assert spec.design_method == "ofat"
        assert any("ofat" in note for note in spec.metadata["lab"].get("notes", []))
