"""Property 15: steps after midnight are priced with their own date.

**Validates: Requirements 6.2, 6.3, 6.4, 6.5**
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from wattwise.adapters import household_model as HM
from wattwise.adapters import tariff_model as TM

STEP = HM.STEP_H


@st.composite
def date_dependent_tariffs(draw):
    """Every (month group, day type) combination gets its own partition of 24 h with distinct prices and labels."""
    group_a = sorted(draw(st.sets(st.integers(1, 12), min_size=1, max_size=11)))
    groups = {"A": group_a, "B": [m for m in range(1, 13) if m not in group_a]}
    periods = []
    for g, months in groups.items():
        for dt in ("weekday", "weekend"):
            cuts = sorted(draw(st.sets(st.integers(1, 23), max_size=3)))
            bounds = [0, *cuts, 24]
            for i, (s, e) in enumerate(zip(bounds, bounds[1:])):
                periods.append({"label": f"{g}-{dt}-{i}", "start": s, "end": e, "months": months, "days": dt,
                                "price": draw(st.floats(0.01, 1, allow_nan=False))})
    return {"id": "t", "name": "t", "demand_charge_per_kw": 0.0, "periods": periods}


# Fridays and month ends are over-represented so the midnight crossings that matter are exercised.
_any_date = st.dates(date(2024, 1, 1), date(2027, 12, 31))
_fridays = _any_date.map(lambda d: d + timedelta(days=(4 - d.weekday()) % 7))
_month_ends = st.tuples(st.integers(2024, 2027), st.integers(1, 12)).map(
    lambda ym: (date(ym[0] + ym[1] // 12, ym[1] % 12 + 1, 1) - timedelta(days=1)))
start_dates = st.one_of(_any_date, _fridays, _month_ends)

abs_hours = st.lists(st.integers(0, 4 * 72 - 1).map(lambda i: i * STEP), min_size=1, max_size=96)


def _expected_period(tariff, abs_hour, start):
    """Independent lookup: the period whose hours, month and day type match the calendar date of the step."""
    d = start + timedelta(days=math.floor(abs_hour / 24))
    hod = abs_hour % 24
    day_type = "weekend" if d.weekday() >= 5 else "weekday"
    hits = [p for p in tariff["periods"] if p["start"] <= hod < p["end"] and p["days"] == day_type and d.month in p["months"]]
    assert len(hits) == 1
    return hits[0]


@pytest.mark.parametrize("start", [date(2025, 1, 3), date(2025, 1, 31), date(2025, 12, 31)])
def test_friday_and_month_end_examples(start):
    tariff = {"id": "t", "name": "t", "periods": [
        {"label": "wd", "start": 0, "end": 24, "months": [1, 12], "days": "weekday", "price": 0.1},
        {"label": "we", "start": 0, "end": 24, "months": [1, 12], "days": "weekend", "price": 0.2},
        {"label": "other", "start": 0, "end": 24, "months": [2, 3, 4, 5, 6, 7, 8, 9, 10, 11], "days": "all", "price": 0.3}]}
    ctx = TM.window_context(tariff, start)
    # hour 30 is 06:00 on the next calendar day
    nxt = start + timedelta(days=1)
    expected = {"wd": 0.1, "we": 0.2, "other": 0.3}
    label = "other" if nxt.month in range(2, 12) else ("we" if nxt.weekday() >= 5 else "wd")
    assert HM.period_label(tariff, 30.0, ctx) == label
    assert HM.price_at(tariff, 30.0, ctx) == expected[label]


# Feature: wattwise-household-model-improvements, Property 15: Post-midnight steps use their own date
@settings(max_examples=100, deadline=None)
@given(tariff=date_dependent_tariffs(), start=start_dates, hours=abs_hours, kw=st.floats(0.1, 8, allow_nan=False))
def test_post_midnight_steps_use_their_own_date(tariff, start, hours, kw):
    ctx = TM.window_context(tariff, start)
    assert ctx is not None
    for h in hours:
        exp = _expected_period(tariff, h, start)
        assert HM.price_at(tariff, h, ctx) == exp["price"]
        assert HM.period_label(tariff, h, ctx) == exp["label"]

    readings = [(h, kw) for h in hours]
    summary = HM.summarise(readings, tariff, ctx=ctx)
    by_label: dict[str, dict[str, float]] = {}
    for h, k in readings:
        p = _expected_period(tariff, h, start)
        d = by_label.setdefault(p["label"], {"kwh": 0.0, "cost": 0.0})
        d["kwh"] += k * STEP
        d["cost"] += k * STEP * p["price"]
    got = summary["cost_by_period"]
    assert set(got) == set(by_label)
    for label, d in by_label.items():
        assert got[label]["kwh"] == pytest.approx(d["kwh"])
        assert got[label]["cost"] == pytest.approx(d["cost"])
    assert summary["energy_charge"] == pytest.approx(sum(d["cost"] for d in by_label.values()))
