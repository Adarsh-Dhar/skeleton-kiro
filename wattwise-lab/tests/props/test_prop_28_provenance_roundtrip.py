"""Property 7: provenance round trip.
**Validates: Requirements 3.8, 3.10**
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from wattwise.services.meter_library import PROVENANCE_LABELS, MeterLibrary

T0 = datetime(2024, 1, 1)

labels = st.sampled_from(PROVENANCE_LABELS)
values = st.lists(st.floats(min_value=0, max_value=20, allow_nan=False, allow_infinity=False).map(lambda x: round(x, 3)), min_size=2, max_size=20)
OPTS = {"timestamp_column": "timestamp", "value_column": "kwh", "reading_kind": "interval_energy", "unit": "kWh"}


def _csv(vals: list[float]) -> str:
    rows = ["timestamp,kwh"] + [f"{(T0 + timedelta(minutes=30 * i)).isoformat()},{v!r}" for i, v in enumerate(vals)]
    return "\n".join(rows) + "\n"


def _engine(d: str, target: int | None = None):
    from db import create_db_engine, migrate

    engine = create_db_engine(Path(d) / "trialforge.db")
    migrate(engine, target=target)
    return engine


# Feature: wattwise-household-model-improvements, Property 7: Provenance round trip
@settings(max_examples=100, deadline=None)
@given(vals=values, label=labels)
def test_stored_label_is_read_back(vals, label):
    with tempfile.TemporaryDirectory() as d:
        engine = _engine(d)
        try:
            lib = MeterLibrary(engine)
            meta = lib.import_csv(_csv(vals), "p7.csv", dict(OPTS), provenance=label)
            assert meta["source_label"] == label
            assert lib.get(meta["import_id"])["source_label"] == label
            listed = {m["import_id"]: m for m in lib.list()}
            assert listed[meta["import_id"]]["source_label"] == label
        finally:
            engine.dispose()


# Feature: wattwise-household-model-improvements, Property 7: Provenance round trip
@settings(max_examples=100, deadline=None)
@given(vals=values, label=labels)
def test_pre_migration_row_reads_back_unknown_and_other_fields_unchanged(vals, label):
    from sqlalchemy import text

    with tempfile.TemporaryDirectory() as d:
        engine = _engine(d, target=10)
        try:
            with engine.begin() as c:
                c.execute(text(
                    "INSERT INTO ww_meter_import (import_id, file_name, imported_at, source, timezone, reading_kind, unit, interval_minutes, first_ts_utc, "
                    "last_ts_utc, rows_total, rows_used, rows_skipped, content_sha256, options_json, warnings_json, row_log_json, gaps_json) VALUES "
                    "('mi-old', 'old.csv', '2023-01-01 00:00:00', 'csv upload', 'UTC', 'interval_energy', 'kWh', 30.0, '2023-01-01T00:00:00', "
                    "'2023-01-01T01:00:00', :n, :n, 0, 'abc', '{}', '[]', '[]', '[]')"), {"n": len(vals)})
            from db import migrate

            migrate(engine)
            lib = MeterLibrary(engine)
            old = lib.get("mi-old")
            assert old["source_label"] == "unknown"
            assert (old["file_name"], old["source"], old["timezone"], old["reading_kind"], old["unit"]) == ("old.csv", "csv upload", "UTC", "interval_energy", "kWh")
            assert (old["interval_minutes"], old["first_utc"], old["last_utc"], old["rows_total"], old["rows_used"], old["rows_skipped"]) == (
                30.0, "2023-01-01T00:00:00", "2023-01-01T01:00:00", len(vals), len(vals), 0)
            # a new import alongside the legacy row still round-trips
            meta = lib.import_csv(_csv(vals), "new.csv", dict(OPTS), provenance=label)
            assert meta["source_label"] == label
            assert lib.get("mi-old")["source_label"] == "unknown"
        finally:
            engine.dispose()
