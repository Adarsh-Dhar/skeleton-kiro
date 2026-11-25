"""Property 21: invalid-interval readings are excluded without side effects.
**Validates: Requirements 8.4**
"""
from __future__ import annotations
import copy
from datetime import UTC, datetime, timedelta
from hypothesis import given, settings
from hypothesis import strategies as st
from wattwise.adapters import meter_import as MI

BASE = datetime(2024, 1, 1, tzinfo=UTC)
VALID = st.sampled_from([5.0, 15.0, 30.0, 60.0])
INVALID = st.sampled_from([0.0, -1.0, -30.0, float("nan"), float("inf"), float("-inf")])


@st.composite
def reading_lists(draw):
    flags = draw(st.lists(st.booleans(), min_size=0, max_size=30))
    out = []
    for i, bad in enumerate(flags):
        iv = draw(INVALID if bad else VALID)
        out.append(MI.Reading(start_utc=BASE + timedelta(minutes=15 * i), interval_minutes=iv,
                              kwh=draw(st.floats(0, 10)), kw=draw(st.floats(0, 40)),
                              original_ts=f"ts{i}", source_row=i + 2, flags=[]))
    return out


def _ok(r):
    return r.interval_minutes == r.interval_minutes and abs(r.interval_minutes) != float("inf") and r.interval_minutes > 0


# Feature: wattwise-household-model-improvements, Property 21: Invalid-interval readings are excluded without side effects
@settings(max_examples=100, deadline=None)
@given(reading_lists())
def test_invalid_interval_readings_excluded_without_side_effects(readings):
    snapshot = copy.deepcopy(readings)
    log = MI._Log()
    out = MI._drop_bad_intervals(readings, log)
    valid = [r for r in readings if _ok(r)]
    invalid = [r for r in readings if not _ok(r)]
    # remaining readings are exactly the valid ones, unchanged and in order
    assert out == valid
    # importing the list without the invalid ones gives the same result and an empty log
    clean_log = MI._Log()
    assert MI._drop_bad_intervals(valid, clean_log) == out
    assert clean_log.rows == [] and clean_log.skipped == 0
    # each invalid reading is logged as skipped, naming its timestamp
    assert log.skipped == len(invalid)
    assert len(log.rows) == len(invalid)
    for r, row in zip(invalid, log.rows):
        assert row["action"] == "skipped"
        assert r.start_utc.isoformat() in row["reason"]
    # inputs are not mutated
    assert len(readings) == len(snapshot)
    for a, b in zip(readings, snapshot):
        assert (a.start_utc, a.kwh, a.kw, a.flags, a.source_row) == (b.start_utc, b.kwh, b.kw, b.flags, b.source_row)
