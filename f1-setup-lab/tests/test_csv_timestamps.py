"""CSV-imported laps must not carry an invented date (previously every import was stamped 2026-05-01 13:00)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from f1lab.adapters.telemetry import parse_telemetry_csv, to_measurements

CSV = "lap,sector_1,sector_2,sector_3\n1,28,35,27\n2,28.1,35.1,27.1\n3,28.2,35.2,27.2\n"


def test_import_time_is_used_and_flagged_not_a_fixed_date():
    before = datetime.now(UTC)
    ms = to_measurements("r1", parse_telemetry_csv(CSV, "s"), "csv:x")
    after = datetime.now(UTC)
    assert all(before - timedelta(seconds=1) <= m.timestamp <= after for m in ms if m.metadata["lap"] == 3)
    assert not any(m.timestamp.year == 2026 and m.timestamp.month == 5 and m.timestamp.day == 1 for m in ms)
    assert all("import_time_assumed" in m.quality_flags for m in ms)


def test_explicit_base_time_and_lap_order_are_respected():
    base = datetime(2026, 3, 3, 18, 0, tzinfo=UTC)
    ms = to_measurements("r1", parse_telemetry_csv(CSV, "s"), "csv:x", base_time=base)
    by_lap = {m.metadata["lap"]: m.timestamp for m in ms if m.metric == "lap_time"}
    assert by_lap[3] == base and by_lap[1] < by_lap[2] < by_lap[3]
