"""Property 19: billing-cycle default.

**Validates: Requirements 7.6, 7.7**
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from wattwise.adapters.meter_import import Reading
from wattwise.services.baseline import billing_cycle_default

ZONES = ["UTC", "Asia/Kolkata", "America/New_York", "Europe/London"]


@st.composite
def readings_list(draw):
    n = draw(st.integers(0, 40))
    base = datetime(2025, 1, 1, tzinfo=UTC)
    out = []
    for i in range(n):
        start = base + timedelta(minutes=15 * draw(st.integers(0, 24 * 4 * 120)))
        kwh = draw(st.integers(0, 5000)) / 1000
        out.append(Reading(start_utc=start, interval_minutes=15, kwh=kwh, kw=kwh * 4, original_ts=start.isoformat(), source_row=i))
    return out


days = st.dates(min_value=date(2025, 1, 1), max_value=date(2025, 5, 31))


# Feature: wattwise-household-model-improvements, Property 19: Billing-cycle default
@settings(max_examples=100, deadline=None)
@given(rs=readings_list(), day=days, tz=st.sampled_from(ZONES))
def test_billing_cycle_default_sums_earlier_readings_in_month(rs, day, tz):
    zone = ZoneInfo(tz)
    expected = [
        r.kwh
        for r in rs
        if (d := r.start_utc.astimezone(zone).date()).year == day.year and d.month == day.month and d < day
    ]
    out = billing_cycle_default(rs, tz, day)
    if expected:
        assert out["kwh"] == pytest.approx(sum(expected), abs=1e-3 + 1e-9 * len(expected))
        assert out["notice"] is None
    else:
        assert out["kwh"] == 0.0
        assert out["notice"]


# Feature: wattwise-household-model-improvements, Property 19: Billing-cycle default
@settings(max_examples=100, deadline=None)
@given(day=days)
def test_billing_cycle_default_unreadable_input_returns_zero_with_notice(day):
    for bad_tz in ("Not/AZone",):
        out = billing_cycle_default([], bad_tz, day)
        assert out["kwh"] == 0.0 and out["notice"]
    out = billing_cycle_default([object()], "UTC", day)  # unreadable reading
    assert out["kwh"] == 0.0 and out["notice"]
