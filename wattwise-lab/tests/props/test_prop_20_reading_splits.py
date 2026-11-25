"""Property 20: reading splits conserve energy and cost.

**Validates: Requirements 8.1, 8.2, 8.3, 8.6, 8.7**
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from wattwise.adapters import tariff_model as TM


@st.composite
def flat_tariffs(draw):
    """A tariff whose periods partition 0-24 h (no tiers, no wrap), with random prices."""
    cuts = sorted(draw(st.lists(st.integers(1, 23), max_size=5, unique=True)))
    edges = [0, *cuts, 24]
    periods = [
        {"label": f"P{i}", "start": s, "end": e, "price": draw(st.floats(0, 1, allow_nan=False))}
        for i, (s, e) in enumerate(zip(edges, edges[1:]))
    ]
    return {"id": "t", "name": "t", "periods": periods}


readings = st.tuples(
    st.datetimes(min_value=datetime(2025, 1, 1), max_value=datetime(2025, 12, 31)).map(lambda d: d.replace(microsecond=0)),
    st.integers(1, 3 * 86400),  # duration in seconds, spans up to three local midnights
    st.floats(0.001, 500, allow_nan=False),
)


def _price_at(tariff, when: datetime) -> float:
    hod = when.hour + when.minute / 60 + when.second / 3600 + when.microsecond / 3.6e9
    return next(p["price"] for p in tariff["periods"] if p["start"] <= hod < p["end"])


# Feature: wattwise-household-model-improvements, Property 20: Reading splits conserve energy and cost
@settings(max_examples=100, deadline=None)
@given(tariff=flat_tariffs(), reading=readings, other=readings)
def test_reading_splits_conserve_energy_and_cost(tariff, reading, other):
    start, duration, kwh = reading
    slices = TM.price_reading(tariff, start, duration, kwh)

    # coverage: contiguous, no gaps or overlaps, exactly the reading's interval
    assert slices[0].start == start
    for a, b in zip(slices, slices[1:]):
        assert a.start + timedelta(seconds=a.seconds) == b.start
    last = slices[-1]
    assert last.start + timedelta(seconds=last.seconds) == start + timedelta(seconds=duration)
    assert sum(s.seconds for s in slices) == pytest.approx(duration, abs=1e-6)

    # energy conservation and proportionality
    assert abs(sum(s.kwh for s in slices) - kwh) <= 1e-9
    for s in slices:
        assert s.kwh == pytest.approx(kwh * s.seconds / duration, rel=1e-12, abs=1e-12)

    # cost equals slice energy times an independent price lookup at the slice midpoint
    expected = sum(s.kwh * _price_at(tariff, s.start + timedelta(seconds=s.seconds / 2)) for s in slices)
    assert sum(s.charge for s in slices) == pytest.approx(expected, rel=1e-9, abs=1e-9)
    assert all(s.credit == 0.0 for s in slices)

    # independence from other readings and no cost for gaps: pricing another reading first changes nothing,
    # and a reading's slices never extend beyond its own interval
    TM.price_reading(tariff, *other)
    again = TM.price_reading(tariff, start, duration, kwh)
    assert again == slices
    assert TM.price_reading(tariff, start, 0, kwh) == []
