"""Shared Hypothesis generators for the CSV import property tests (f1-unified-telemetry-import)."""
from __future__ import annotations

import csv
import io

from hypothesis import strategies as st

# Standard channels (other than time) that a Column_Mapping may omit.
CHANNEL_FIELDS = ["distance_m", "speed_kph", "throttle_pct", "brake_pct", "steer_pct", "gear", "rpm", "drs", "lap_number"]
PLAIN_CHANNELS = ["throttle_pct", "brake_pct", "steer_pct", "gear", "rpm", "drs"]

# Cells that must never be read as numbers.
bad_cells = st.sampled_from(["", " ", "   ", "\t", "abc", "n/a", "NaN", "--", "1,2x", "inf"])
# Integer cells, rendered the way a CSV would carry them.
good_numbers = st.integers(min_value=0, max_value=5000)
# Lap cell values that are not integers >= 1 (never "3.0": that parses to the valid integer 3).
invalid_lap_cells = st.sampled_from(["0", "-1", "-7", "1.5", "2.25", "abc", "", " ", "n/a", "NaN", "inf"])
valid_laps = st.integers(min_value=1, max_value=200)


def to_csv_text(columns: dict[str, list[str]]) -> str:
    """Columns (name -> list of cell strings, equal length) -> CSV text."""
    names = list(columns)
    n = len(columns[names[0]]) if names else 0
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(names)
    for i in range(n):
        w.writerow([columns[c][i] for c in names])
    return buf.getvalue()


@st.composite
def time_column(draw, n: int | None = None, min_size: int = 3, max_size: int = 15):
    """Strictly increasing, unique session times in seconds (as strings) so no row is dropped or reordered."""
    size = n if n is not None else draw(st.integers(min_value=min_size, max_value=max_size))
    steps = draw(st.lists(st.integers(min_value=1, max_value=50), min_size=size, max_size=size))
    t, out = 0, []
    for s in steps:
        t += s
        out.append(str(t))
    return out


@st.composite
def cell_with_badness(draw, n: int):
    """(cells, bad_mask): n cell strings; masked positions are empty / whitespace / non-numeric, the rest integers."""
    mask = draw(st.lists(st.booleans(), min_size=n, max_size=n))
    cells = [draw(bad_cells) if bad else str(draw(good_numbers)) for bad in mask]
    return cells, mask


@st.composite
def lap_cells(draw, n: int):
    """(cells, valid_mask): lap cells where valid ones are integers >= 1 and the others are not."""
    mask = draw(st.lists(st.booleans(), min_size=n, max_size=n))
    cells = [str(draw(valid_laps)) if ok else draw(invalid_lap_cells) for ok in mask]
    return cells, mask
