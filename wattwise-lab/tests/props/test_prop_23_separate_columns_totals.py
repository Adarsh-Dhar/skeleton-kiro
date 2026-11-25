"""Property 23: separate import and export columns are preserved and totalled.
**Validates: Requirements 9.1, 9.6**
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from wattwise.adapters.meter_import import ImportOptions, interpret
from wattwise.services.meter_library import MeterLibrary

T0 = datetime(2024, 1, 1)

kwh = st.floats(min_value=0, max_value=50, allow_nan=False, allow_infinity=False).map(lambda x: round(x, 3))
pairs = st.lists(st.tuples(kwh, kwh), min_size=2, max_size=60)


def _csv(pairs_: list[tuple[float, float]]) -> str:
    rows = ["timestamp,imp,exp"]
    rows += [f"{(T0 + timedelta(minutes=30 * i)).isoformat()},{a!r},{b!r}" for i, (a, b) in enumerate(pairs_)]
    return "\n".join(rows) + "\n"


OPTS = {"timestamp_column": "timestamp", "value_column": "imp", "export_column": "exp", "reading_kind": "interval_energy", "unit": "kWh"}


# Feature: wattwise-household-model-improvements, Property 23: Separate import and export columns are preserved and totalled
@settings(max_examples=100, deadline=None)
@given(data=pairs)
def test_separate_columns_preserved(data):
    readings = interpret(_csv(data), ImportOptions(**OPTS)).readings
    assert len(readings) == len(data)
    for r, (a, b) in zip(readings, data, strict=True):
        assert abs(r.kwh - a) < 5e-4 and abs(r.export_kwh - b) < 5e-4


# Feature: wattwise-household-model-improvements, Property 23: Separate import and export columns are preserved and totalled
@settings(max_examples=100, deadline=None)
@given(data=pairs)
def test_library_totals_equal_stored_sums(data):
    from db import create_db_engine, migrate

    with tempfile.TemporaryDirectory() as d:
        engine = create_db_engine(Path(d) / "trialforge.db")
        migrate(engine)
        try:
            lib = MeterLibrary(engine)
            meta = lib.import_csv(_csv(data), "p23.csv", dict(OPTS))
            stored = lib.readings(meta["import_id"])
            assert len(stored) == len(data)
            for r, (a, b) in zip(stored, data, strict=True):
                assert abs(r.kwh - a) < 5e-4 and abs(r.export_kwh - b) < 5e-4
            assert abs(meta["total_import_kwh"] - sum(r.kwh for r in stored)) < 1e-6
            assert abs(meta["total_export_kwh"] - sum(r.export_kwh for r in stored)) < 1e-6
        finally:
            engine.dispose()
