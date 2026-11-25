"""Property tests for tyre degradation (Requirements 6.2, 6.3, design D8)."""
from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from f1lab.integrations.historical.analysis import FEW_LAPS_REASON, tyre_degradation

DRIVER = "VER"


def _lap(n: int, age: int, time_s: float, **over) -> dict:
    row = {
        "driver": DRIVER, "stint": 1, "lap_number": n, "tyre_life": age,
        "lap_time_s": time_s, "is_valid": True, "is_deleted": False,
        "pits": False, "compound": "SOFT",
    }
    row.update(over)
    return row


def _clean(a: float, b: float, noise: list[float]) -> list[dict]:
    return [_lap(i + 1, i + 1, a + b * (i + 1) + e) for i, e in enumerate(noise)]


def _stint(result: dict) -> dict:
    assert len(result["stints"]) == 1
    return result["stints"][0]


# Feature: f1-historical-phase1, Property 6: Degradation recovery
@settings(max_examples=150, deadline=None)
@given(
    a=st.floats(60, 120),
    b=st.floats(-0.5, 0.5),
    noise=st.lists(st.floats(-0.02, 0.02), min_size=5, max_size=40),
)
def test_property_6_slope_recovery_with_noise(a, b, noise):
    """**Validates: Requirements 6.2**"""
    s = _stint(tyre_degradation(_clean(a, b, noise)))
    assert s["reason"] is None
    assert s["laps_used"] == len(noise)
    assert abs(s["slope_s_per_lap"] - b) <= 0.05


# Feature: f1-historical-phase1, Property 6: Degradation recovery (zero noise)
@settings(max_examples=150, deadline=None)
@given(a=st.floats(60, 120), b=st.floats(-0.5, 0.5), n=st.integers(5, 40))
def test_property_6_slope_exact_without_noise(a, b, n):
    """**Validates: Requirements 6.2**"""
    s = _stint(tyre_degradation(_clean(a, b, [0.0] * n)))
    assert s["slope_s_per_lap"] == pytest.approx(b, abs=1e-6)


# Feature: f1-historical-phase1, Property 6: Degradation recovery (too few laps)
@settings(max_examples=150, deadline=None)
@given(
    a=st.floats(60, 120),
    b=st.floats(-0.5, 0.5),
    n=st.integers(0, 4),
    extras=st.integers(0, 6),
)
def test_property_6_fewer_than_five_usable_laps(a, b, n, extras):
    """**Validates: Requirements 6.3**"""
    laps = _clean(a, b, [0.0] * n)
    laps += [_lap(n + 1 + i, n + 1 + i, 90.0, is_valid=False) for i in range(extras)]
    result = tyre_degradation(laps)
    if not laps:
        assert result["stints"] == []
        return
    s = _stint(result)
    assert s["slope_s_per_lap"] is None
    assert s["r_squared"] is None
    assert s["reason"] == FEW_LAPS_REASON == "fewer than 5 usable laps"


extreme_time = st.one_of(st.floats(1e3, 1e6), st.floats(-1e6, -1e3), st.floats(0.0, 1.0))
KINDS = st.sampled_from(["invalid", "deleted", "pit", "safety_car"])


# Feature: f1-historical-phase1, Property 7: Exclusions do not change the slope
@settings(max_examples=150, deadline=None)
@given(
    a=st.floats(60, 120),
    b=st.floats(-0.5, 0.5),
    noise=st.lists(st.floats(-0.02, 0.02), min_size=5, max_size=30),
    bad=st.lists(st.tuples(KINDS, extreme_time), min_size=1, max_size=10),
)
def test_property_7_excluded_laps_do_not_change_slope(a, b, noise, bad):
    """**Validates: Requirements 6.2**"""
    base_laps = _clean(a, b, noise)
    base = _stint(tyre_degradation(base_laps))
    n = len(noise)
    laps = list(base_laps)
    sc_laps = []
    for i, (kind, t) in enumerate(bad):
        ln = n + 1 + i
        over = {
            "invalid": {"is_valid": False},
            "deleted": {"is_deleted": True},
            "pit": {"pits": True},
            "safety_car": {},
        }[kind]
        if kind == "safety_car":
            sc_laps.append(ln)
        laps.append(_lap(ln, ln, t, **over))
    rc = []
    for ln in sc_laps:
        rc.append({"lap_number": ln, "message": "SAFETY CAR DEPLOYED"})
        rc.append({"lap_number": ln, "message": "SAFETY CAR IN THIS LAP"})
    result = _stint(tyre_degradation(laps, rc))
    assert result["laps_used"] == base["laps_used"]
    assert result["slope_s_per_lap"] == pytest.approx(base["slope_s_per_lap"], abs=1e-9)
    assert result["r_squared"] == pytest.approx(base["r_squared"], abs=1e-9)
