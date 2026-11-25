"""Property tests for the compass conversion (Requirement 2.5, design D6)."""
from __future__ import annotations

import json
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from f1lab.integrations.historical.analysis import compass

FIXTURE = Path(__file__).parent / "fixtures" / "compass_cases.json"
NAMES = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def sector(m: float) -> str:
    """Independent oracle: explicit range checks on a bearing in [0, 360)."""
    if m >= 337.5 or m < 22.5:
        return "N"
    for i in range(1, 8):
        if 45 * i - 22.5 <= m < 45 * i + 22.5:
            return NAMES[i]
    raise AssertionError(m)


bearings = st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False)


# Feature: f1-historical-phase1, Property 1: Compass sector
@settings(max_examples=200, deadline=None)
@given(bearings)
def test_compass_equals_sector_of_mod_360(d):
    assert compass(d) == sector(d % 360)


# Feature: f1-historical-phase1, Property 1: Compass sector
@settings(max_examples=200, deadline=None)
@given(st.floats(min_value=0, max_value=1e4, allow_nan=False, allow_infinity=False))
def test_compass_periodic_over_360(d):
    # Compare against the oracle on the shifted value too: float addition of 360
    # can move d across a boundary, so periodicity is checked via d mod 360.
    assert compass(d + 360) == sector((d + 360) % 360)
    assert compass(d) == sector(d % 360)


# Feature: f1-historical-phase1, Property 1: Compass sector
def test_compass_boundaries():
    assert compass(22.5) == "NE"
    assert compass(22.4) == "N"
    assert compass(337.5) == "N"
    assert compass(337.4) == "NW"
    assert compass(360) == "N"
    assert compass(359.99) == "N"
    assert compass(0) == "N"


# Feature: f1-historical-phase1, Property 1: Compass sector
def test_compass_shared_fixture_cases():
    data = json.loads(FIXTURE.read_text())
    assert len(data["cases"]) >= 30
    for case in data["cases"]:
        assert compass(case["deg"]) == case["compass"], case
