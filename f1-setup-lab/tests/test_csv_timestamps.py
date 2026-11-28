"""CSV-imported laps keep file timestamps unmodified; rows without one are unavailable (no invented time or 90 s spacing)."""
from __future__ import annotations

from datetime import UTC, datetime

from f1lab.adapters.telemetry import UNAVAILABLE_TIMESTAMP, parse_telemetry_csv, to_measurements
from f1lab.integrations.historical.providers import PROVENANCE_DEMO

CSV = "lap,sector_1,sector_2,sector_3\n1,28,35,27\n2,28.1,35.1,27.1\n3,28.2,35.2,27.2\n"
CSV_TS = "lap,timestamp,sector_1,sector_2,sector_3\n1,2026-03-03T18:00:00Z,28,35,27\n2,2026-03-03T18:01:07.250Z,28,35,27\n3,,28,35,27\n"


def test_missing_timestamps_are_unavailable_not_computed():
    ms = to_measurements("r1", parse_telemetry_csv(CSV, "s"), "csv:x")
    assert ms and all(m.timestamp == UNAVAILABLE_TIMESTAMP for m in ms)
    assert all(m.metadata["timestamp_unavailable"] is True and "timestamp_unavailable" in m.quality_flags for m in ms)


def test_file_timestamps_are_kept_unmodified():
    ms = to_measurements("r1", parse_telemetry_csv(CSV_TS, "s"), "csv:x")
    by_lap = {m.metadata["lap"]: m for m in ms if m.metric == "lap_time"}
    assert by_lap[1].timestamp == datetime(2026, 3, 3, 18, 0, tzinfo=UTC)
    assert by_lap[2].timestamp == datetime(2026, 3, 3, 18, 1, 7, 250000, tzinfo=UTC)
    assert "timestamp_unavailable" not in by_lap[1].metadata
    assert by_lap[3].metadata["timestamp_unavailable"] is True


def test_sector_only_file_has_no_speed_rpm_brake_or_stability():
    metrics = {m.metric for m in to_measurements("r1", parse_telemetry_csv(CSV, "s"), "csv:x")}
    assert metrics == {"sector_1", "sector_2", "sector_3", "lap_time"}


def test_flags_follow_provenance_status():
    ms = to_measurements("r1", parse_telemetry_csv(CSV, "s"), "demo", PROVENANCE_DEMO)
    assert all(m.metadata["synthetic"] is True and m.metadata["imported"] is False for m in ms)


def test_unparseable_file_is_rejected():
    p = parse_telemetry_csv("lap,sector_1,sector_2,sector_3\n1,\"28,35,27\n")
    assert not p.laps and p.errors
