"""Property 24: invalid export-mode values reject the whole file.
**Validates: Requirements 9.4, 9.5**
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import text

from db import create_db_engine, migrate
from wattwise.adapters.meter_import import ImportOptions, MeterImportError, interpret
from wattwise.services.meter_library import MeterLibrary

T0 = datetime(2024, 1, 1)
_DIR = tempfile.mkdtemp(prefix="prop24_")
_ENGINE = create_db_engine(Path(_DIR) / "trialforge.db")
migrate(_ENGINE)
_LIB = MeterLibrary(_ENGINE)


def _counts() -> tuple[int, int]:
    with _ENGINE.connect() as c:
        return (c.execute(text("SELECT COUNT(*) FROM ww_meter_import")).scalar(),
                c.execute(text("SELECT COUNT(*) FROM ww_meter_reading")).scalar())


@st.composite
def bad_files(draw):
    mode = draw(st.sampled_from(["separate", "signed"]))
    n = draw(st.integers(min_value=2, max_value=40))
    bad_row = draw(st.integers(min_value=0, max_value=n - 1))
    if mode == "separate":
        cols = ["import_kwh", "export_kwh"]
        bad_col = draw(st.sampled_from(cols))
        defect = draw(st.sampled_from(["negative", "text"]))
    else:
        cols = ["kwh"]
        bad_col = "kwh"
        defect = "text"  # a negative signed value is valid (it is export)
    if defect == "negative":
        bad_val = str(-draw(st.integers(min_value=1, max_value=5000)) / 1000)
    else:
        bad_val = draw(st.sampled_from(["abc", "n/a", "--", "1.2.3", "x"]))
    good = st.integers(min_value=0, max_value=5000).map(lambda v: f"{v / 1000:.3f}")
    rows = []
    for i in range(n):
        vals = {c: draw(good) for c in cols}
        if i == bad_row:
            vals[bad_col] = bad_val
        rows.append(vals)
    header = ["timestamp", *cols]
    lines = [",".join(header)]
    for i, vals in enumerate(rows):
        lines.append(",".join([(T0 + timedelta(minutes=30 * i)).isoformat(), *[vals[c] for c in cols]]))
    return mode, "\n".join(lines) + "\n", bad_row + 2, bad_col  # data row 0 is file line 2


def _opts(mode: str) -> dict:
    if mode == "separate":
        return {"timestamp_column": "timestamp", "value_column": "import_kwh", "export_column": "export_kwh",
                "reading_kind": "interval_energy", "unit": "kWh"}
    return {"timestamp_column": "timestamp", "value_column": "kwh", "signed": True, "reading_kind": "interval_energy", "unit": "kWh"}


# Feature: wattwise-household-model-improvements, Property 24: Invalid export-mode values reject the whole file
@settings(max_examples=100, deadline=None)
@given(f=bad_files())
def test_invalid_value_rejects_whole_file(f):
    mode, csv_text, line, col = f
    before = _counts()
    with pytest.raises(MeterImportError) as ei:
        interpret(csv_text, ImportOptions(**_opts(mode)))
    msg = str(ei.value)
    assert f"row {line}," in msg and f"'{col}'" in msg
    with pytest.raises(MeterImportError) as ei2:
        _LIB.import_csv(csv_text, "bad.csv", _opts(mode))
    assert f"row {line}," in str(ei2.value) and f"'{col}'" in str(ei2.value)
    assert _counts() == before
