"""Property 22: signed column split.
**Validates: Requirements 9.2, 9.3, 9.9**
"""
from __future__ import annotations

from datetime import datetime, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from wattwise.adapters.meter_import import ImportOptions, interpret

T0 = datetime(2024, 1, 1)


def _csv(values: list[float]) -> str:
    rows = ["timestamp,kwh"]
    rows += [f"{(T0 + timedelta(minutes=30 * i)).isoformat()},{v!r}" for i, v in enumerate(values)]
    return "\n".join(rows) + "\n"


def _run(values: list[float]):
    opt = ImportOptions(timestamp_column="timestamp", value_column="kwh", reading_kind="interval_energy", unit="kWh", signed=True)
    return interpret(_csv(values), opt).readings


signed_values = st.lists(st.floats(min_value=-50, max_value=50, allow_nan=False, allow_infinity=False).map(lambda x: round(x, 3)),
                         min_size=2, max_size=60)
nonneg_values = st.lists(st.floats(min_value=0, max_value=50, allow_nan=False, allow_infinity=False).map(lambda x: round(x, 3)),
                         min_size=2, max_size=60)


# Feature: wattwise-household-model-improvements, Property 22: Signed column split
@settings(max_examples=100, deadline=None)
@given(values=signed_values)
def test_signed_split(values):
    readings = _run(values)
    assert len(readings) == len(values)
    for r, v in zip(readings, values, strict=True):
        if v > 0:
            assert abs(r.kwh - v) < 1e-9 and r.export_kwh == 0
        elif v < 0:
            assert abs(r.export_kwh - abs(v)) < 1e-9 and r.kwh == 0
        else:
            assert r.kwh == 0 and r.export_kwh == 0
    net = sum(r.kwh for r in readings) - sum(r.export_kwh for r in readings)
    assert abs(net - sum(values)) <= 0.001


# Feature: wattwise-household-model-improvements, Property 22: Signed column split
@settings(max_examples=100, deadline=None)
@given(values=nonneg_values)
def test_import_only_has_zero_export(values):
    readings = _run(values)
    assert all(r.export_kwh == 0 and r.export_kw == 0 for r in readings)
