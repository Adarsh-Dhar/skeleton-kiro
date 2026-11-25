"""Property 27: Overlap detection blocks every contained appliance.
**Validates: Requirements 10.2**
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from wattwise.services.baseline import ModelledLoad, check_overlap

BLOCK = 18  # slots per injected occurrence; the load sits at offset 6 so both neighbour windows are load-free
OFFSET = 6
START = datetime(2024, 1, 1, tzinfo=UTC)


@st.composite
def scenarios(draw):
    n = draw(st.integers(min_value=1, max_value=5))
    # Added appliances draw 1-3 kW; appliances left out draw 4-6 kW so no injected load can mimic them.
    flags = draw(st.lists(st.booleans(), min_size=n, max_size=n))
    loads = []
    for i, added in enumerate(flags):
        power = draw(st.floats(1.0, 3.0) if added else st.floats(4.0, 6.0))
        steps = draw(st.integers(1, 6))
        loads.append(ModelledLoad(f"app{i}", power, steps))
    occurrences = {
        i: draw(st.integers(3, 5)) for i, added in enumerate(flags) if added
    }
    total_blocks = sum(occurrences.values()) + 1
    noise = draw(st.lists(st.floats(0.0, 0.1), min_size=total_blocks * BLOCK, max_size=total_blocks * BLOCK))
    return loads, flags, occurrences, noise


# Feature: wattwise-household-model-improvements, Property 27: Overlap detection blocks every contained appliance
@settings(max_examples=100, deadline=None)
@given(scenarios())
def test_overlap_reports_exactly_the_contained_appliances(scenario):
    loads, flags, occurrences, noise = scenario
    values = list(noise)
    block = 0
    for i, count in occurrences.items():
        for _ in range(count):
            base = block * BLOCK + OFFSET
            for k in range(loads[i].steps):
                values[base + k] += loads[i].power_kw
            block += 1
    series = [(START + timedelta(minutes=15 * j), v) for j, v in enumerate(values)]
    found = check_overlap(series, loads)
    assert sorted(found) == sorted(m.name for m, added in zip(loads, flags) if added)
    # Detection is a pure read: the series is not modified.
    assert [v for _t, v in series] == values
