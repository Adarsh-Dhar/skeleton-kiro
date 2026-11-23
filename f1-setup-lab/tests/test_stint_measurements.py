"""Accepted stint -> SDK measurements."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from f1lab.integrations.game_udp.capture import StintResult
from f1lab.integrations.game_udp.measurements import stint_to_measurements
from f1lab.integrations.game_udp.protocols.f1_25 import LapRecord

END = datetime(2026, 3, 3, 20, 0, tzinfo=UTC)


def laps(times):
    return [LapRecord(i + 3, t, (t * 0.31, t * 0.39, t * 0.30), True, (True, True, True)) for i, t in enumerate(times)]


def test_per_lap_and_run_level_measurements():
    r = StintResult(laps([90.1, 89.9, 90.0]), True, [], 2, 2, 5)
    ms = stint_to_measurements("run-1", r, 318.4, end_time=END, evidence="e.json")
    lap_t = [m for m in ms if m.metric == "lap_time"]
    assert [m.value for m in lap_t] == [90.1, 89.9, 90.0] and {m.metadata["lap"] for m in lap_t} == {3, 4, 5} and len(ms) == 3 * 4 + 3
    run_level = {m.metric: m for m in ms if "lap" not in m.metadata}
    assert run_level["valid_laps"].value == 3 and run_level["valid_laps"].unit == "count"
    assert run_level["lap_time_std"].value == pytest.approx(0.1, abs=1e-3) and run_level["top_speed"].value == 318.4 and run_level["top_speed"].unit == "km/h"
    assert all(m.metadata["imported"] is True and m.metadata["synthetic"] is False and m.metadata["evidence"] == "e.json" and m.run_id == "run-1" for m in ms)


def test_timestamps_run_backwards_from_the_stint_end_and_are_marked_derived():
    ms = stint_to_measurements("r", StintResult(laps([90.0, 91.0, 89.0]), True, [], 1, 2, 5), 0.0, end_time=END)
    t = {m.metadata["lap"]: m.timestamp for m in ms if m.metric == "lap_time"}
    assert t[5] == END and (t[4] - t[3]).total_seconds() == pytest.approx(91.0) and (t[5] - t[4]).total_seconds() == pytest.approx(89.0)
    assert all(m.metadata["timestamp_derived"] for m in ms if m.metadata.get("lap"))
    assert not [m for m in ms if m.metric == "top_speed"]  # no speed data, no invented value


def test_single_lap_has_zero_spread():
    ms = stint_to_measurements("r", StintResult(laps([90.0]), True, [], 1, 2, 3), 300.0, end_time=END)
    assert next(m for m in ms if m.metric == "lap_time_std").value == 0.0
