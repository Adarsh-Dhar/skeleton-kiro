"""Property tests for mini-sectors (Requirements 5.2, 5.3, 5.4, 5.6, design D7)."""
from __future__ import annotations

import math

import numpy as np
import pytest
from hypothesis import given, settings, strategies as st

from f1lab.errors import Conflict
from f1lab.integrations.historical.analysis import mini_sectors

steps = st.floats(min_value=0.01, max_value=50, allow_nan=False, allow_infinity=False)
starts = st.floats(min_value=0, max_value=1000, allow_nan=False, allow_infinity=False)


@st.composite
def laps(draw, n=None):
    n = n if n is not None else draw(st.integers(min_value=20, max_value=200))
    d = draw(starts) + np.cumsum([0.0] + draw(st.lists(steps, min_size=n - 1, max_size=n - 1)))
    t = draw(starts) + np.cumsum([0.0] + draw(st.lists(steps, min_size=n - 1, max_size=n - 1)))
    return {"distance_m": d.tolist(), "session_time_s": t.tolist()}


counts = st.integers(min_value=10, max_value=50)


# Feature: f1-historical-phase1, Property 3: Mini-sector time sum
@settings(max_examples=200, deadline=None)
@given(laps(), counts)
def test_slice_times_sum_to_lap_time(lap, count):
    out = mini_sectors(lap, count)
    times = [s["time_s"] for s in out["slices"]]
    lap_time = lap["session_time_s"][-1] - lap["session_time_s"][0]
    assert len(out["slices"]) == count
    assert all(x >= 0 for x in times)
    assert abs(sum(times) - lap_time) <= 1e-9
    assert abs(out["lap_time_s"] - lap_time) <= 1e-9
    assert abs(out["sum_slices_s"] - lap_time) <= 1e-9


# Feature: f1-historical-phase1, Property 4: Reference antisymmetry
@settings(max_examples=200, deadline=None)
@given(laps(), laps(), counts)
def test_reference_swap_negates_deltas(a, b, count):
    ab = mini_sectors(a, count, b)["slices"]
    ba = mini_sectors(b, count, a)["slices"]
    assert len(ab) == len(ba) == count
    for x, y in zip(ab, ba):
        assert math.isclose(x["delta_s"], -y["delta_s"], abs_tol=1e-9)
        assert math.isclose(x["cum_delta_s"], -y["cum_delta_s"], abs_tol=1e-9)


@st.composite
def bad_laps(draw):
    n = draw(st.integers(min_value=20, max_value=200))
    k = draw(st.integers(min_value=n // 10 + 1, max_value=n - 1))  # > 10 percent
    bad_idx = set(draw(st.permutations(range(1, n)))[:k])
    channel = draw(st.sampled_from(["distance_m", "session_time_s"]))
    good = draw(laps(n))
    vals = [0.0]
    for i in range(1, n):
        delta = draw(steps)
        vals.append(vals[-1] + (0.0 if i in bad_idx else delta))
    good[channel] = vals
    return good, channel


# Feature: f1-historical-phase1, Property 5: Rejection of non-increasing telemetry
@settings(max_examples=200, deadline=None)
@given(bad_laps(), counts)
def test_non_increasing_channel_raises_conflict_naming_channel(case, count):
    lap, channel = case
    with pytest.raises(Conflict) as exc:
        mini_sectors(lap, count)
    assert channel in str(exc.value)
