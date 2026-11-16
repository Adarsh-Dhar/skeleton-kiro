"""Telemetry CSV parsing: recorded laps/sectors -> SDK measurements (tagged as imported, not synthetic)."""

from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from trialforge import Measurement

from f1lab.adapters.vehicle_model import METRIC_UNITS

MAX_ROWS = 5000
MAX_BYTES = 1_000_000
EXTRA = ("stability", "top_speed", "tyre_degradation")


@dataclass
class ParsedTelemetry:
    laps: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    time_unit: str = "s"


def parse_telemetry_csv(text: str, time_unit: str = "s") -> ParsedTelemetry:
    """Parse ``lap,sector_1,sector_2,sector_3[,lap_time,stability,top_speed,tyre_degradation]``.

    Rows with problems are reported in ``errors`` (file line numbers) and skipped; the rest are kept.
    ``lap_time`` defaults to the sector sum. ``time_unit`` is ``s`` or ``ms`` (converted by the SDK).
    """
    if time_unit not in ("s", "ms"):
        raise ValueError("time_unit must be 's' or 'ms'")
    out = ParsedTelemetry(time_unit=time_unit)
    if len(text.encode()) > MAX_BYTES:
        out.errors.append(f"file larger than {MAX_BYTES} bytes")
        return out
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if not reader.fieldnames:
        out.errors.append("empty file")
        return out
    names = {h.strip().lower(): h for h in reader.fieldnames}
    missing = [c for c in ("sector_1", "sector_2", "sector_3") if c not in names]
    if missing:
        out.errors.append(f"missing required column(s): {', '.join(missing)}")
        return out

    def num(row: dict, col: str, line: int) -> float | None:
        raw = (row.get(names[col]) or "").strip() if col in names else ""
        if raw == "":
            return None
        try:
            v = float(raw)
        except ValueError:
            raise ValueError(f"line {line}: '{col}' is not a number ({raw!r})") from None
        if not math.isfinite(v) or v < 0:
            raise ValueError(f"line {line}: '{col}' must be a finite, non-negative number")
        return v

    for i, row in enumerate(reader, start=2):
        if len(out.laps) >= MAX_ROWS:
            out.errors.append(f"only the first {MAX_ROWS} rows were read")
            break
        try:
            sectors = [num(row, f"sector_{k}", i) for k in (1, 2, 3)]
            if any(s is None for s in sectors):
                raise ValueError(f"line {i}: sector times are required")
            lap_time = num(row, "lap_time", i)
            lap_no = num(row, "lap", i)
            rec = {"lap": int(lap_no) if lap_no is not None else len(out.laps) + 1, "sector_1": sectors[0],
                   "sector_2": sectors[1], "sector_3": sectors[2],
                   "lap_time": lap_time if lap_time is not None else sum(sectors)}  # fmt: skip
            for extra in EXTRA:
                v = num(row, extra, i)
                if v is not None:
                    rec[extra] = v
            out.laps.append(rec)
        except ValueError as exc:
            out.errors.append(str(exc))
    if not out.laps and not out.errors:
        out.errors.append("no data rows")
    return out


def to_measurements(run_id: str, parsed: ParsedTelemetry, source: str) -> list[Measurement]:
    """Build SDK measurements. Times carry the file's unit; the SDK converts ms -> s keeping the raw value."""
    t0 = datetime(2026, 5, 1, 13, 0, tzinfo=UTC)
    ms: list[Measurement] = []
    unit = "ms" if parsed.time_unit == "ms" else "seconds"
    for rec in parsed.laps:
        ts = t0 + timedelta(seconds=90 * rec["lap"])
        meta = {"synthetic": False, "imported": True, "lap": rec["lap"]}
        for metric in ("sector_1", "sector_2", "sector_3", "lap_time"):
            ms.append(Measurement(run_id=run_id, metric=metric, value=rec[metric], unit=unit, timestamp=ts,
                                  source=source, metadata=dict(meta)))  # fmt: skip
        for extra in EXTRA:
            if extra in rec:
                ms.append(Measurement(run_id=run_id, metric=extra, value=rec[extra], unit=METRIC_UNITS[extra],
                                      timestamp=ts, source=source, metadata=dict(meta)))  # fmt: skip
    return ms
