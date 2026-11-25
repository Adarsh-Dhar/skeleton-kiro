"""Property 12: day-type profiles are per-slot means over the complete days of that type.

**Validates: Requirements 5.1, 5.2, 5.5**
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from wattwise.adapters.meter_import import Reading
from wattwise.services import baseline as BL

SLOTS = 96

# January/February 2025 has no DST transition in any of these zones, so every local day has 96 slots.
ZONES = ["UTC", "Asia/Kolkata", "Asia/Kathmandu", "America/New_York", "Europe/Berlin", "Pacific/Auckland", "America/St_Johns"]


@st.composite
def meter_series(draw):
    zone = draw(st.sampled_from(ZONES))
    start = date(2025, 1, 1) + timedelta(days=draw(st.integers(0, 30)))
    n_days = draw(st.integers(1, 14))
    days = []
    for i in range(n_days):
        mode = draw(st.sampled_from(["full", "full", "gaps", "partial"]))
        kw = draw(st.lists(st.floats(0, 6, allow_nan=False, allow_infinity=False), min_size=SLOTS, max_size=SLOTS))
        if mode == "full":
            present = set(range(SLOTS))
        elif mode == "gaps":  # at most 4 missing slots: coverage >= 92/96 >= 0.95, still a complete day
            missing = draw(st.sets(st.integers(0, SLOTS - 1), max_size=4))
            present = set(range(SLOTS)) - missing
        else:  # first 40 slots only: an incomplete day, excluded from every profile
            present = set(range(40))
        days.append((start + timedelta(days=i), kw, present, mode != "partial"))
    return zone, days


def _readings(zone, days):
    tz = ZoneInfo(zone)
    out = []
    for d, kw, present, _ in days:
        midnight = datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(UTC)
        for k in sorted(present):
            out.append(Reading(midnight + timedelta(minutes=15 * k), 15.0, kw[k] * 0.25, kw[k], "", len(out)))
    return out


def _expected(days, wanted):
    """Independent oracle: per-slot mean of the slot's kW over complete days (of the wanted types) that have the slot."""
    chosen = [(kw, present) for d, kw, present, complete in days
              if complete and (wanted is None or ("weekend" if d.weekday() >= 5 else "weekday") == wanted)]
    if not chosen:
        return None, 0
    prof = []
    for k in range(SLOTS):
        vals = [kw[k] for kw, present in chosen if k in present]
        prof.append(sum(vals) / len(vals) if vals else None)
    return prof, len(chosen)


def _assert_profile(got, exp):
    if exp is None:
        assert got is None
        return
    assert len(got) == SLOTS
    for g, e in zip(got, exp):
        if e is None:
            assert g is None
        else:
            assert g == pytest.approx(e, abs=1e-3)


def test_weekend_is_saturday_and_sunday():
    assert [BL.day_type_of_date(date(2025, 1, 6) + timedelta(days=i)) for i in range(7)] == ["weekday"] * 5 + ["weekend"] * 2


# Feature: wattwise-household-model-improvements, Property 12: Day-type profiles are per-slot means
@settings(max_examples=100, deadline=None)
@given(series=meter_series())
def test_day_type_profiles_are_per_slot_means(series):
    zone, days = series
    analysis = BL.analyse(_readings(zone, days), zone)
    for key, wanted in (("all", None), ("weekday", "weekday"), ("weekend", "weekend")):
        exp, count = _expected(days, wanted)
        _assert_profile(analysis["profiles"][key], exp)
        assert analysis["day_counts"][key] == count
        if key != "all" and count >= 1:
            sel = BL.select_profile(analysis, key)
            assert sel["fallback"] is False and sel["day_type_used"] == key and sel["days_used"] == count
            _assert_profile(sel["profile_kw"], exp)
