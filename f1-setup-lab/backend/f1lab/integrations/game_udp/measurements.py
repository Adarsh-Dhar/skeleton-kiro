"""Accepted stint -> SDK measurements (stint-level only; raw packets stay in the raw recording)."""

from __future__ import annotations

import statistics
from datetime import UTC, datetime, timedelta

from trialforge import Measurement

from f1lab.integrations.game_udp.capture import StintResult

SOURCE = "ea-sports-f1-25-udp"


def stint_to_measurements(run_id: str, result: StintResult, top_speed_kph: float, *, end_time: datetime | None = None, evidence: str = "") -> list[Measurement]:
    """Per-lap lap_time and sector_1..3 (keeps the analysis code unchanged) + valid_laps, lap_time_std and top_speed for the stint.

    Lap timestamps are derived backwards from the stint end time using the lap durations; they are approximate (flagged in metadata).
    """
    end = end_time or datetime.now(UTC)
    laps = result.laps
    out: list[Measurement] = []
    after = 0.0
    stamps: dict[int, datetime] = {}
    for lap in reversed(laps):
        stamps[lap.lap] = end - timedelta(seconds=after)
        after += lap.lap_time_s
    base = {"synthetic": False, "imported": True, "source_kind": "game_udp", "evidence": evidence}
    for lap in laps:
        meta = {**base, "lap": lap.lap, "timestamp_derived": True}
        for metric, value in (("lap_time", lap.lap_time_s), ("sector_1", lap.sectors_s[0]), ("sector_2", lap.sectors_s[1]), ("sector_3", lap.sectors_s[2])):
            out.append(Measurement(run_id=run_id, metric=metric, value=round(value, 4), unit="seconds", timestamp=stamps[lap.lap], source=SOURCE, metadata=dict(meta)))
    std = statistics.stdev([lap.lap_time_s for lap in laps]) if len(laps) >= 2 else 0.0
    run_level = [("valid_laps", len(laps), "count"), ("lap_time_std", round(std, 4), "seconds")]
    if top_speed_kph > 0:
        run_level.append(("top_speed", round(top_speed_kph, 1), "km/h"))
    for metric, value, unit in run_level:
        out.append(Measurement(run_id=run_id, metric=metric, value=value, unit=unit, timestamp=end, source=SOURCE, metadata=dict(base)))
    return out
