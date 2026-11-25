"""Property 13: day-type profiles recombine to the all-days profile.
**Validates: Requirements 5.8**
"""
from __future__ import annotations
from datetime import date, timedelta
from hypothesis import given, settings
from hypothesis import strategies as st
from wattwise.services import baseline as B

SLOTS = 96
BASE = date(2026, 3, 2)  # a Monday


@st.composite
def day_slots(draw):
    """Per complete day, a sparse slot map {slot: [kwh, minutes]}; slots may be missing."""
    present = draw(st.sets(st.integers(0, SLOTS - 1), max_size=SLOTS))
    return {k: [draw(st.floats(0.0, 5.0, allow_nan=False)), draw(st.sampled_from([5.0, 10.0, 15.0]))] for k in present}


@st.composite
def series(draw):
    offsets = sorted(draw(st.sets(st.integers(0, 40), min_size=1, max_size=21)))
    days = [BASE + timedelta(days=o) for o in offsets]
    return {d: draw(day_slots()) for d in days}, days


# Feature: wattwise-household-model-improvements, Property 13: Day-type profiles recombine to the all-days profile
@settings(max_examples=100, deadline=None)
@given(series())
def test_day_type_profiles_recombine_to_all_days(data):
    per_day, complete = data
    prof = B.profiles(per_day, complete)
    assert prof["all"] is not None and len(prof["all"]) == SLOTS

    def weight(d, k):
        e = per_day[d].get(k)
        return 1 if e and e[1] > 0 else 0

    for k in range(SLOTS):
        num = den = 0.0
        for kind in B.DAY_TYPES:
            ds = [d for d in complete if B.day_type_of_date(d) == kind]
            if not ds or prof[kind] is None:  # only day types with >= 1 complete day
                continue
            w = sum(weight(d, k) for d in ds)
            if w and prof[kind][k] is not None:
                num += w * prof[kind][k]
                den += w
        if den == 0:
            assert prof["all"][k] is None
        else:
            assert prof["all"][k] is not None
            assert abs(num / den - prof["all"][k]) <= 1e-9
