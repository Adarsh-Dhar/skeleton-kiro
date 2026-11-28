"""Telemetry CSV parsing: recorded laps/sectors -> SDK measurements.

Parsing is delegated to ``csv_normalizer.parse_csv`` (the same entry point the CSV Upload provider uses, Req 14.2). Timestamps come
from the file untouched (Req 14.3); a row without one is marked unavailable and never gets a computed time (Req 14.4). The
``synthetic``/``imported`` flags are derived from the Provenance_Status (Req 13.6).
"""

from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime

from trialforge import Measurement

from f1lab.adapters.vehicle_model import METRIC_UNITS
from f1lab.integrations.historical import csv_normalizer
from f1lab.integrations.historical.providers import PROVENANCE_DEMO, PROVENANCE_USER, guard_synthetic

MAX_ROWS = 5000
MAX_BYTES = 1_000_000
EXTRA = ("stability", "top_speed", "tyre_degradation")
SECTORS = ("sector_1", "sector_2", "sector_3")
# Placeholder for Measurement.timestamp (a required datetime) when the file has none; flagged ``timestamp_unavailable``.
UNAVAILABLE_TIMESTAMP = datetime(1970, 1, 1, tzinfo=UTC)
_CANON = {"lap": "lap_number", "lap_time": "lap_time_s", "sector_1": "sector_1_s", "sector_2": "sector_2_s", "sector_3": "sector_3_s"}


@dataclass
class ParsedTelemetry:
    laps: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    time_unit: str = "s"


def parse_telemetry_csv(text: str, time_unit: str = "s") -> ParsedTelemetry:
    """Parse ``lap,sector_1,sector_2,sector_3[,lap_time,timestamp,stability,top_speed,tyre_degradation]`` via the shared normalizer.

    Rows with problems are reported in ``errors`` (file line numbers) and skipped; the rest are kept. ``lap_time`` defaults to the
    sector sum. A file the normalizer cannot parse yields no laps and one "could not be parsed" error. ``time_unit`` is ``s`` or ``ms``;
    values keep the file unit and the SDK converts ms -> s (keeping the raw value).
    """
    if time_unit not in ("s", "ms"):
        raise ValueError("time_unit must be 's' or 'ms'")
    out = ParsedTelemetry(time_unit=time_unit)
    if len(text.encode()) > MAX_BYTES:
        out.errors.append(f"file larger than {MAX_BYTES} bytes")
        return out
    text = text.lstrip("\ufeff")
    try:
        reader = csv.DictReader(io.StringIO(text))
        header = reader.fieldnames
        raw_rows = list(reader) if header else []
    except csv.Error as exc:
        out.errors.append(f"file could not be parsed: {exc}")
        return out
    if not header:
        out.errors.append("empty file")
        return out
    names = {h.strip().lower(): h for h in header if h}
    missing = [c for c in SECTORS if c not in names]
    if missing:
        out.errors.append(f"missing required column(s): {', '.join(missing)}")
        return out
    mapping = {_CANON[k]: names[k].strip() for k in _CANON if k in names}
    try:
        parsed = csv_normalizer.parse_csv(text, mapping, {"time": "seconds"},  # values stay in the file unit; the SDK converts ms -> s
                                          
                                          {"data_type": csv_normalizer.LAP_SUMMARY})
    except Exception as exc:  # unparseable input: reject, nothing else is touched
        out.errors.append(f"file could not be parsed: {exc}")
        return out
    if parsed.blocking_errors:
        out.errors.append("file could not be parsed: " + "; ".join(str(e["column"]) + " " + str(e["reason"]) for e in parsed.blocking_errors))
        return out
    by_row: dict[int, list[dict]] = {}
    for issue in parsed.issues:
        by_row.setdefault(issue["row"], []).append(issue)
    back = {v: k for k, v in mapping.items()}
    laps = parsed.laps

    def problem(row_no: int, line: int) -> str | None:
        for issue in sorted(by_row.get(row_no, []), key=lambda x: str(x["column"])):
            field_name = {v: k for k, v in _CANON.items()}.get(back.get(issue["column"], ""), "")
            if field_name in ("lap", "lap_time") and issue["reason"] == "empty" or issue["reason"] == "invalid_lap_number" and str(issue["value"]).strip() == "":
                continue
            if field_name in SECTORS and issue["reason"] == "empty":
                return f"line {line}: sector times are required"
            if issue["reason"] == "non_numeric" or issue["reason"] == "invalid_lap_number":
                return f"line {line}: '{field_name}' is not a number ({str(issue['value']).strip()!r})"
            try:
                negative = float(str(issue["value"]).strip()) < 0
            except ValueError:
                negative = True
            return f"line {line}: '{field_name}' must be a finite, non-negative number" if negative else f"line {line}: '{field_name}' must be greater than zero"
        return None

    def extra(raw: dict, col: str, line: int) -> float | None:
        cell = (raw.get(names[col]) or "").strip() if col in names else ""
        if cell == "":
            return None
        try:
            v = float(cell)
        except ValueError:
            raise ValueError(f"line {line}: '{col}' is not a number ({cell!r})") from None
        if not math.isfinite(v) or v < 0:
            raise ValueError(f"line {line}: '{col}' must be a finite, non-negative number")
        return v

    for i in range(len(laps)):
        if len(out.laps) >= MAX_ROWS:
            out.errors.append(f"only the first {MAX_ROWS} rows were read")
            break
        line, raw = i + 2, raw_rows[i] if i < len(raw_rows) else {}
        err = problem(i + 1, line)
        if err:
            out.errors.append(err)
            continue
        try:
            extras = {k: extra(raw, k, line) for k in EXTRA}
        except ValueError as exc:
            out.errors.append(str(exc))
            continue
        sectors = [float(laps[f"sector_{k}_s"].iloc[i]) for k in (1, 2, 3)]
        lap_time = laps["lap_time_s"].iloc[i]
        lap_no = laps["lap_number"].iloc[i]
        rec: dict = {"lap": int(lap_no) if not _isna(lap_no) else len(out.laps) + 1,
                     "sector_1": sectors[0], "sector_2": sectors[1], "sector_3": sectors[2],
                     "lap_time": float(lap_time) if not _isna(lap_time) else sum(sectors)}  # fmt: skip
        rec.update({k: v for k, v in extras.items() if v is not None})
        raw_ts = (raw.get(names["timestamp"]) or "").strip() if "timestamp" in names else ""
        ts = csv_normalizer.parse_timestamp(raw_ts) if raw_ts else None
        rec["timestamp"] = ts
        if ts is not None:
            rec["timestamp_raw"] = raw_ts
        out.laps.append(rec)
    if not out.laps and not out.errors:
        out.errors.append("no data rows")
    return out


def _isna(v: object) -> bool:
    return v is None or str(v) in ("<NA>", "nan", "NaT")


def to_measurements(run_id: str, parsed: ParsedTelemetry, source: str, provenance_status: str = PROVENANCE_USER) -> list[Measurement]:
    """Build SDK measurements. Times carry the file's unit (``ms`` or seconds); the SDK converts ms -> s keeping the raw value.

    ``synthetic``/``imported`` follow ``provenance_status`` (demo data is synthetic and guarded as such; everything else is
    imported). A timestamp read from the file is used as is; a row without one gets ``timestamp_unavailable`` metadata and the
    ``timestamp_unavailable`` quality flag, and no time is computed for it. Only the columns present become measurements, so a
    sector-only file yields no speed, RPM, brake or stability measurements.
    """
    synthetic = guard_synthetic("demo" if provenance_status == PROVENANCE_DEMO else "upload", provenance_status == PROVENANCE_DEMO)
    ms: list[Measurement] = []
    unit = "ms" if parsed.time_unit == "ms" else "seconds"
    for rec in parsed.laps:
        ts = rec.get("timestamp")
        meta = {"synthetic": synthetic, "imported": not synthetic, "provenance_status": provenance_status, "lap": rec["lap"]}
        flags: list[str] = []
        if ts is None:
            meta["timestamp_unavailable"] = True
            flags.append("timestamp_unavailable")
            ts = UNAVAILABLE_TIMESTAMP
        else:
            meta["timestamp_raw"] = rec.get("timestamp_raw")
        for metric in ("sector_1", "sector_2", "sector_3", "lap_time"):
            ms.append(Measurement(run_id=run_id, metric=metric, value=rec[metric], unit=unit, timestamp=ts, source=source,
                                  metadata=dict(meta), quality_flags=list(flags)))  # fmt: skip
        for extra in EXTRA:
            if extra in rec:
                ms.append(Measurement(run_id=run_id, metric=extra, value=rec[extra], unit=METRIC_UNITS[extra], timestamp=ts,
                                      source=source, metadata=dict(meta), quality_flags=list(flags)))  # fmt: skip
    return ms
