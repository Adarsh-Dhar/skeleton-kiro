"""Property 25: canonical format round trip.

**Validates: Requirements 9.10, 9.11**
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from wattwise.adapters.meter_import import CANONICAL_OPTIONS, format_canonical, interpret

TOL = 0.001


@st.composite
def canonical_files(draw):
    """Regular-interval import/export files with >= 2 readings (a single reading cannot be re-parsed)."""
    n = draw(st.integers(min_value=2, max_value=48))
    interval = draw(st.sampled_from([5, 15, 30, 60]))
    start = draw(st.datetimes(min_value=datetime(2020, 1, 1), max_value=datetime(2030, 1, 1)).map(lambda d: d.replace(second=0, microsecond=0, tzinfo=UTC)))
    imp = draw(st.lists(st.floats(0, 50, allow_nan=False, allow_infinity=False), min_size=n, max_size=n))
    exp = draw(st.lists(st.floats(0, 50, allow_nan=False, allow_infinity=False), min_size=n, max_size=n))
    lines = ["timestamp,import_kwh,export_kwh"]
    for i in range(n):
        ts = (start + timedelta(minutes=interval * i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines.append(f"{ts},{imp[i]:.4f},{exp[i]:.4f}")
    return "\n".join(lines) + "\n"


# Feature: wattwise-household-model-improvements, Property 25: Canonical format round trip
@settings(max_examples=100, deadline=None)
@given(canonical_files())
def test_canonical_round_trip(text):
    first = interpret(text, CANONICAL_OPTIONS)
    again = interpret(format_canonical(first.readings), CANONICAL_OPTIONS)
    assert len(again.readings) == len(first.readings)
    for a, b in zip(first.readings, again.readings, strict=True):
        assert a.start_utc == b.start_utc
        assert abs(a.kwh - b.kwh) <= TOL
        assert abs(a.export_kwh - b.export_kwh) <= TOL
