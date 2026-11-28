"""Property tests for unavailable channels and lap assignment (f1-unified-telemetry-import, Properties 7, 8, 14, 15)."""
from __future__ import annotations

import math

import pandas as pd
from csv_strategies import (
    CHANNEL_FIELDS,
    PLAIN_CHANNELS,
    cell_with_badness,
    good_numbers,
    lap_cells,
    time_column,
    to_csv_text,
)
from hypothesis import given, settings
from hypothesis import strategies as st

from f1lab.integrations.historical.csv_normalizer import assign_laps, build_telemetry_frame, parse_csv

SETTINGS = settings(max_examples=100, deadline=None)


def _is_missing(v) -> bool:
    return v is None or v is pd.NA or (isinstance(v, float) and math.isnan(v))


# Feature: f1-unified-telemetry-import, Property 7: Unmapped standard fields are never populated
# **Validates: Requirements 3.4**
@SETTINGS
@given(data=st.data(), omitted=st.sampled_from(CHANNEL_FIELDS))
def test_property_7_unmapped_fields_never_populated(data, omitted):
    times = data.draw(time_column())
    n = len(times)
    columns = {"time": times}
    mapping = {"session_time_s": "time"}
    for field in CHANNEL_FIELDS:
        # Every channel has a similarly named column full of valid values, e.g. `speed_kph`, `Speed_KPH_raw`.
        raw = [str(data.draw(st.integers(min_value=1, max_value=200))) for _ in range(n)]
        columns[field] = raw
        columns[f"{field}_raw"] = raw
        if field != omitted:
            mapping[field] = field
    parsed = parse_csv(to_csv_text(columns), mapping)
    tel = parsed.telemetry
    assert len(tel) == n
    assert all(_is_missing(v) for v in tel[omitted].tolist())
    assert omitted in parsed.unavailable
    for field in CHANNEL_FIELDS:
        if field != omitted:
            assert field not in parsed.unavailable
            assert not tel[field].isna().any()


# Feature: f1-unified-telemetry-import, Property 8: Bad or empty mapped-channel cells are stored as missing, never filled
# **Validates: Requirements 3.5**
@SETTINGS
@given(data=st.data(), channel=st.sampled_from(PLAIN_CHANNELS + ["speed_kph", "distance_m"]))
def test_property_8_bad_cells_stored_as_missing(data, channel):
    times = data.draw(time_column(min_size=3))
    n = len(times)
    cells, mask = data.draw(cell_with_badness(n))
    text = to_csv_text({"time": times, "chan": cells})
    parsed = parse_csv(text, {"session_time_s": "time", channel: "chan"})
    values = parsed.telemetry[channel].tolist()
    assert len(values) == n
    for i, (cell, bad) in enumerate(zip(cells, mask)):
        if bad:
            assert _is_missing(values[i])  # never interpolated or copied from a neighbour
        else:
            assert not _is_missing(values[i])
    # Unit-free channels keep good cells exactly as supplied.
    if channel in PLAIN_CHANNELS:
        for i, (cell, bad) in enumerate(zip(cells, mask)):
            if not bad:
                assert values[i] == float(cell)
    # every bad cell was reported
    reported = {i["row"] for i in parsed.issues if i["column"] == "chan"}
    assert reported == {i + 1 for i, bad in enumerate(mask) if bad}


# Feature: f1-unified-telemetry-import, Property 14: Lap assignment is never inferred without a source
# **Validates: Requirements 5.9**
@SETTINGS
@given(
    data=st.data(),
    with_lap_summary=st.booleans(),
    lap_column_present_but_unmapped=st.booleans(),
)
def test_property_14_laps_never_inferred(data, with_lap_summary, lap_column_present_but_unmapped):
    times = data.draw(time_column(min_size=2))
    n = len(times)
    speeds = [str(data.draw(good_numbers)) for _ in range(n)]
    columns = {"time": times, "speed": speeds}
    if lap_column_present_but_unmapped:
        columns["lap_number"] = [str(i % 3 + 1) for i in range(n)]
    text = to_csv_text(columns)
    mapping = {"session_time_s": "time", "speed_kph": "speed"}

    parsed = parse_csv(text, mapping)
    assert len(parsed.telemetry) == n
    assert parsed.telemetry["lap_number"].isna().all()
    assert "lap_number" in parsed.unavailable

    frame = pd.DataFrame(columns)
    summary = pd.DataFrame({"lap_number": [1, 2, 3], "lap_time_s": [90.0, 91.0, 92.0]}) if with_lap_summary else None
    invalid: list = []
    laps = assign_laps(frame, None, summary, invalid)
    assert len(laps) == n and laps.isna().all()
    assert invalid == []

    built = build_telemetry_frame(frame, mapping)
    assert built["lap_number"].isna().all()


# Feature: f1-unified-telemetry-import, Property 15: Mapped lap_number values are assigned verbatim or reported invalid
# **Validates: Requirements 5.10**
@SETTINGS
@given(data=st.data())
def test_property_15_mapped_laps_verbatim_or_reported(data):
    times = data.draw(time_column(min_size=2))
    n = len(times)
    cells, valid = data.draw(lap_cells(n))

    # assign_laps directly
    invalid: list = []
    laps = assign_laps(pd.DataFrame({"lap": cells}), "lap", None, invalid)
    expected_bad = [(i + 1, cells[i]) for i in range(n) if not valid[i]]
    assert [(r, str(v)) for r, v in invalid] == expected_bad
    for i in range(n):
        if valid[i]:
            assert laps.iloc[i] == int(cells[i])
        else:
            assert _is_missing(laps.iloc[i])

    # end to end through parse_csv: same assignment, invalid rows reported with row number and original value
    parsed = parse_csv(to_csv_text({"time": times, "lap": cells}), {"session_time_s": "time", "lap_number": "lap"})
    got = parsed.telemetry["lap_number"].tolist()
    for i in range(n):
        if valid[i]:
            assert got[i] == int(cells[i])
        else:
            assert _is_missing(got[i])
    reported = [(i["row"], str(i["value"])) for i in parsed.issues if i["reason"] == "invalid_lap_number"]
    assert reported == expected_bad
