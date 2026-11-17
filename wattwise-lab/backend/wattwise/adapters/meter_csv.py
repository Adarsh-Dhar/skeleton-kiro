"""Smart-meter CSV parsing: readings -> SDK measurements (tagged imported, never synthetic)."""

from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from trialforge import Measurement

MAX_ROWS = 20000
MAX_BYTES = 2_000_000
POWER_COLS = {"power_kw": "kW", "power_w": "W", "power": None, "kw": "kW", "w": "W"}


@dataclass
class ParsedMeter:
    readings: list[tuple[datetime, float]] = field(default_factory=list)  # (timestamp, value in `unit`)
    unit: str = "kW"
    errors: list[str] = field(default_factory=list)


def parse_meter_csv(text: str, unit: str = "kW") -> ParsedMeter:
    """Parse ``timestamp,<power_kw|power_w|power>`` rows (ISO-8601 timestamps). ``power`` uses the ``unit`` argument (kW or W).

    Bad rows are reported (file line numbers) and skipped. Rows are kept in file order (the SDK flags out-of-order/duplicate samples).
    """
    if unit not in ("kW", "W"):
        raise ValueError("unit must be 'kW' or 'W'")
    out = ParsedMeter(unit=unit)
    if len(text.encode()) > MAX_BYTES:
        out.errors.append(f"file larger than {MAX_BYTES} bytes")
        return out
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if not reader.fieldnames:
        out.errors.append("empty file")
        return out
    names = {h.strip().lower(): h for h in reader.fieldnames}
    col = next((c for c in POWER_COLS if c in names), None)
    if "timestamp" not in names or col is None:
        out.errors.append("required columns: 'timestamp' and one of power_kw, power_w, power")
        return out
    if POWER_COLS[col]:
        out.unit = POWER_COLS[col]
    for i, row in enumerate(reader, start=2):
        if len(out.readings) >= MAX_ROWS:
            out.errors.append(f"only the first {MAX_ROWS} rows were read")
            break
        try:
            ts = datetime.fromisoformat((row[names["timestamp"]] or "").strip().replace("Z", "+00:00"))
            ts = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        except ValueError:
            out.errors.append(f"line {i}: invalid timestamp {(row.get(names['timestamp']) or '')!r} (use ISO-8601)")
            continue
        try:
            v = float((row[names[col]] or "").strip())
        except ValueError:
            out.errors.append(f"line {i}: power is not a number ({(row.get(names[col]) or '')!r})")
            continue
        if not math.isfinite(v) or v < 0:
            out.errors.append(f"line {i}: power must be finite and non-negative")
            continue
        out.readings.append((ts, v))
    if not out.readings and not out.errors:
        out.errors.append("no data rows")
    return out


def to_series(run_id: str, parsed: ParsedMeter, source: str) -> list[Measurement]:
    """Grid-power time series (unit kW or W: the SDK converts W to kW, keeping raw values)."""
    return [
        Measurement(run_id=run_id, metric="grid_power", value=v, unit=parsed.unit, timestamp=ts, source=source, channel="grid",
                    metadata={"synthetic": False, "reading_kind": "imported", "sample_index": i})
        for i, (ts, v) in enumerate(parsed.readings)
    ]  # fmt: skip
