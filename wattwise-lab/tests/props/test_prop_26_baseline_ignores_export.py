"""Property 26: baseline ignores export.

**Validates: Requirements 9.7**
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from wattwise.adapters.meter_import import Reading
from wattwise.services import baseline


@st.composite
def reading_pairs(draw):
    n = draw(st.integers(min_value=1, max_value=96 * 3))
    interval = draw(st.sampled_from([15, 30, 60]))
    start = draw(st.datetimes(min_value=datetime(2023, 1, 1), max_value=datetime(2026, 1, 1)).map(lambda d: d.replace(minute=0, second=0, microsecond=0, tzinfo=UTC)))
    imp = draw(st.lists(st.floats(0, 5, allow_nan=False, allow_infinity=False), min_size=n, max_size=n))
    exp_a = draw(st.lists(st.floats(0, 50, allow_nan=False, allow_infinity=False), min_size=n, max_size=n))
    exp_b = draw(st.lists(st.floats(0, 50, allow_nan=False, allow_infinity=False), min_size=n, max_size=n))

    def build(exp):
        return [
            Reading(start + timedelta(minutes=interval * i), interval, imp[i], imp[i] * 60 / interval, "", i + 1, [], exp[i], exp[i] * 60 / interval)
            for i in range(n)
        ]

    return build(exp_a), build(exp_b)


# Feature: wattwise-household-model-improvements, Property 26: Baseline ignores export
@settings(max_examples=100, deadline=None)
@given(reading_pairs())
def test_baseline_ignores_export(pair):
    a, b = pair
    assert baseline.analyse(a, "UTC") == baseline.analyse(b, "UTC")
