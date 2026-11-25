"""Property 28: Subtraction is non-negative and reports clipping.
**Validates: Requirements 10.4, 10.5, 10.8**
"""
from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from wattwise.services.baseline import SLOT_MIN, WINDOW_START, ModelledLoad, subtract_declared

N = 96


@st.composite
def scenarios(draw):
    profile = draw(st.lists(st.floats(0.0, 5.0), min_size=N, max_size=N))
    loads = []
    for i in range(draw(st.integers(0, 4))):
        start = draw(st.integers(0, N - 1))
        if draw(st.booleans()):
            loads.append(ModelledLoad(f"app{i}", draw(st.floats(0.1, 4.0)), draw(st.integers(1, 40)), start))
        else:
            ev = tuple(draw(st.lists(st.floats(0.0, 4.0), min_size=1, max_size=40)))
            loads.append(ModelledLoad(f"ev{i}", 0.0, 0, start, ev))
    return profile, loads


def _clock(i: int) -> str:
    minutes = ((int(WINDOW_START * 60 // SLOT_MIN) + i) * SLOT_MIN) % (24 * 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


# Feature: wattwise-household-model-improvements, Property 28: Subtraction is non-negative and reports clipping
@settings(max_examples=100, deadline=None)
@given(scenarios())
def test_subtraction_is_non_negative_and_reports_clipping(scenario):
    profile, loads = scenario
    original = list(profile)
    result = subtract_declared(profile, loads)

    # Expected raw difference per step (same accumulation order as the implementation).
    raw = list(original)
    for m in loads:
        kws = [m.power_kw] * m.steps if m.profile_kw is None else list(m.profile_kw)
        for k, kw in enumerate(kws):
            raw[(m.start_step + k) % N] -= kw

    assert len(result.profile_kw) == N
    assert all(v >= 0 for v in result.profile_kw)
    negative = [i for i, v in enumerate(raw) if v < 0]
    for i, v in enumerate(raw):
        assert result.profile_kw[i] == (0.0 if v < 0 else v)
    assert result.clipped_count == len(negative)
    assert result.clipped_times == [_clock(i) for i in negative]
    assert result.clipped == {"count": len(negative), "times": [_clock(i) for i in negative]}
    assert result.subtracted == [m.name for m in loads]
    assert profile == original  # input not mutated
