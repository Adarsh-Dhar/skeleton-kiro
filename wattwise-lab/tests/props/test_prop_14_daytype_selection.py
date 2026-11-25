"""Property 14: day-type selection rules.

**Validates: Requirements 5.2, 5.3, 5.4, 5.6**
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from wattwise.services.baseline import InvalidDayType, NoCompleteDay, select_profile

slot_values = st.lists(st.floats(0, 10, allow_nan=False), min_size=96, max_size=96)


@st.composite
def analyses(draw):
    n_weekday = draw(st.integers(0, 20))
    n_weekend = draw(st.integers(0, 10))
    profs = {
        "all": draw(slot_values),
        "weekday": draw(slot_values) if n_weekday else None,
        "weekend": draw(slot_values) if n_weekend else None,
    }
    total = n_weekday + n_weekend
    return {
        "coverage": {"days_total": total + draw(st.integers(0, 3)), "days_complete": total},
        "day_counts": {"all": total, "weekday": n_weekday, "weekend": n_weekend},
        "profiles": profs,
        "profile_15min_kw": profs["all"],
    }


# Feature: wattwise-household-model-improvements, Property 14: Day-type selection rules
@settings(max_examples=100, deadline=None)
@given(analyses(), st.sampled_from(["weekday", "weekend"]))
def test_valid_day_type_selection(analysis, day_type):
    total = analysis["coverage"]["days_complete"]
    count = analysis["day_counts"][day_type]
    if total == 0:
        with pytest.raises(NoCompleteDay):
            select_profile(analysis, day_type)
        return
    out = select_profile(analysis, day_type)
    assert out["requested"] == day_type
    if count >= 1:
        assert out["profile_kw"] == analysis["profiles"][day_type]
        assert out["day_type_used"] == day_type
        assert out["days_used"] == count
        assert out["fallback"] is False
    else:
        assert out["profile_kw"] == analysis["profiles"]["all"]
        assert out["day_type_used"] == "all"
        assert out["days_used"] == total
        assert out["fallback"] is True


# Feature: wattwise-household-model-improvements, Property 14: Day-type selection rules
@settings(max_examples=100, deadline=None)
@given(analyses())
def test_no_day_type_requested_uses_all_days(analysis):
    if analysis["coverage"]["days_complete"] == 0:
        with pytest.raises(NoCompleteDay):
            select_profile(analysis, None)
        return
    out = select_profile(analysis, None)
    assert out["profile_kw"] == analysis["profiles"]["all"]
    assert out["day_type_used"] == "all"
    assert out["days_used"] == analysis["coverage"]["days_complete"]
    assert out["fallback"] is False


# Feature: wattwise-household-model-improvements, Property 14: Day-type selection rules
@settings(max_examples=100, deadline=None)
@given(analyses(), st.text().filter(lambda s: s not in ("weekday", "weekend")))
def test_invalid_day_type_rejected(analysis, day_type):
    with pytest.raises(InvalidDayType):
        select_profile(analysis, day_type)
