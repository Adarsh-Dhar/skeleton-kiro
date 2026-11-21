"""Configurable smart-meter CSV interpretation for the historical meter library.

Every source row ends up in exactly one of: a normalised reading, or the row log with the reason it was skipped. Rows
that were transformed (unit conversion, local time -> UTC, DST resolution, decimal comma, sorting) are logged too, so
the preview shows exactly how the file is interpreted.

Reading kinds
  power            average power over the interval that starts (or ends) at the timestamp   (kW or W)
  interval_energy  energy consumed in the interval that starts (or ends) at the timestamp   (kWh or Wh)
  cumulative       meter register; energy = difference between consecutive readings      (kWh or Wh)
                   a negative difference is NEVER turned into negative consumption: it is either a configured
                   rollover (``rollover_kwh``) or a meter reset/decrease, and that interval is skipped and logged.

Timestamps: ISO-8601 with an offset (or Z) are converted to UTC; naive timestamps are interpreted in ``timezone``
(IANA name). Ambiguous local times in a DST fall-back hour are resolved by file order (first = before the change);
non-existent local times in a spring-forward gap are skipped.
"""
from __future__ import annotations

import csv
import io
import math
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_ROWS = 20000
MAX_BYTES = 2_000_000
ROW_LOG_CAP = 2000

POWER_NAMES = {"power_kw": "kW", "kw": "kW", "power_w": "W", "w": "W", "power": None, "demand_kw": "kW", "watts": "W"}
ENERGY_NAMES = {"energy_kwh": "kWh", "kwh": "kWh", "energy_wh": "Wh", "wh": "Wh", "consumption_kwh": "kWh", "usage_kwh": "kWh", "energy": None}
CUMULATIVE_NAMES = {"meter_reading_kwh": "kWh", "register_kwh": "kWh", "cumulative_kwh": "kWh", "reading_kwh": "kWh", "meter_kwh": "kWh",
                    "meter_reading": None, "register": None, "cumulative": None}  # fmt: skip
TIME_NAMES = ("timestamp", "time", "datetime", "date_time", "interval_start", "start", "read_time", "reading_time", "date")
UNIT_KIND = {"kW": "power", "W": "power", "kWh": "energy", "Wh": "energy"}


class MeterImportError(ValueError):
    pass


@dataclass
class ImportOptions:
    timestamp_column: str | None = None
    value_column: str | None = None
    reading_kind: Literal["power", "interval_energy", "cumulative"] | None = None
    unit: Literal["kW", "W", "kWh", "Wh"] | None = None
    timezone: str = "UTC"
    timestamp_marks: Literal["start", "end"] = "start"
    timestamp_format: str | None = None  # strptime format when not ISO-8601
    delimiter: str | None = None  # auto-detect , ; tab
    decimal: Literal[".", ","] = "."
    interval_minutes: float | None = None  # auto: median spacing
    resample_minutes: float | None = None
    rollover_kwh: float | None = None  # cumulative meters only


@dataclass
class Reading:
    start_utc: datetime
    interval_minutes: float
    kwh: float
    kw: float
    original_ts: str
    source_row: int
    flags: list[str] = field(default_factory=list)


@dataclass
class ImportResult:
    options: dict[str, Any]
    columns: list[str]
    readings: list[Reading]
    rows_total: int
    rows_skipped: int
    row_log: list[dict[str, Any]]
    warnings: list[str]
    gaps: list[dict[str, Any]]
    interval_minutes: float | None

    def summary(self) -> dict[str, Any]:
        kwh = sum(r.kwh for r in self.readings)
        return {"rows_total": self.rows_total, "rows_used": len(self.readings), "rows_skipped": self.rows_skipped,
                "first_utc": self.readings[0].start_utc.isoformat() if self.readings else None,
                "last_utc": self.readings[-1].start_utc.isoformat() if self.readings else None,
                "interval_minutes": self.interval_minutes, "energy_kwh": round(kwh, 4),
                "peak_kw": round(max((r.kw for r in self.readings), default=0.0), 4), "gaps": len(self.gaps),
                "missing_intervals": sum(g["missing_intervals"] for g in self.gaps)}  # fmt: skip


# ----------------------------------------------------------------------------------------------- columns
def _sniff_delimiter(text: str, given: str | None) -> str:
    if given:
        return "\t" if given in ("\\t", "tab") else given
    head = "\n".join(text.splitlines()[:5])
    counts = {d: head.count(d) for d in (",", ";", "\t")}
    return max(counts, key=counts.get) if any(counts.values()) else ","


def resolve_columns(headers: list[str], opt: ImportOptions) -> tuple[str, str, str, str, list[str]]:
    """(timestamp column, value column, reading kind, unit, notes) using the explicit mapping first, then known names."""
    notes: list[str] = []
    lower = {h.strip().lower(): h for h in headers}

    def pick(name: str | None, what: str) -> str | None:
        if name is None:
            return None
        if name in headers:
            return name
        if name.strip().lower() in lower:
            return lower[name.strip().lower()]
        raise MeterImportError(f"{what} column '{name}' is not in the file (columns: {', '.join(headers)})")

    ts_col = pick(opt.timestamp_column, "timestamp") or next((lower[n] for n in TIME_NAMES if n in lower), None)
    if ts_col is None:
        raise MeterImportError(f"no timestamp column found; set timestamp_column (columns: {', '.join(headers)})")
    val_col = pick(opt.value_column, "value")
    kind, unit = opt.reading_kind, opt.unit
    if val_col is None:
        for names, k in ((POWER_NAMES, "power"), (ENERGY_NAMES, "interval_energy"), (CUMULATIVE_NAMES, "cumulative")):
            hit = next((n for n in names if n in lower and lower[n] != ts_col), None)
            if hit:
                val_col = lower[hit]
                kind = kind or k
                unit = unit or names[hit]
                notes.append(f"value column '{val_col}' detected as {kind}")
                break
    if val_col is None:
        raise MeterImportError(f"no value column found; set value_column (columns: {', '.join(headers)})")
    key = val_col.strip().lower()
    if kind is None:
        kind = "power" if key in POWER_NAMES else "interval_energy" if key in ENERGY_NAMES else "cumulative" if key in CUMULATIVE_NAMES else None
    if unit is None:
        unit = {**POWER_NAMES, **ENERGY_NAMES, **CUMULATIVE_NAMES}.get(key)
    if kind is None:
        raise MeterImportError(f"cannot tell what '{val_col}' measures; set reading_kind (power, interval_energy or cumulative)")
    if unit is None:
        raise MeterImportError(f"set unit for '{val_col}' ({'kW or W' if kind == 'power' else 'kWh or Wh'})")
    if (kind == "power") != (UNIT_KIND[unit] == "power"):
        raise MeterImportError(f"unit {unit} does not fit reading_kind {kind}")
    return ts_col, val_col, kind, unit, notes


# ----------------------------------------------------------------------------------------------- timestamps
def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise MeterImportError(f"unknown timezone '{name}' (use an IANA name such as Europe/London)") from exc


def _parse_ts(raw: str, fmt: str | None) -> datetime:
    s = raw.strip()
    if fmt:
        return datetime.strptime(s, fmt)
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _localise(naive: datetime, tz: ZoneInfo, seen_wall: dict[datetime, int]) -> tuple[datetime | None, str | None]:
    """Naive wall-clock time -> aware UTC. Returns (utc or None, flag)."""
    a0 = naive.replace(tzinfo=tz, fold=0)
    a1 = naive.replace(tzinfo=tz, fold=1)
    # non-existent (spring-forward gap): round-tripping through UTC changes the wall time
    if a0.astimezone(UTC).astimezone(tz).replace(tzinfo=None) != naive and a1.astimezone(UTC).astimezone(tz).replace(tzinfo=None) != naive:
        return None, "nonexistent local time (DST spring-forward gap)"
    if a0.utcoffset() != a1.utcoffset():  # ambiguous (fall-back): first occurrence = before the change
        n = seen_wall.get(naive, 0)
        seen_wall[naive] = n + 1
        if n > 1:
            return None, "local time occurs more than twice; cannot resolve DST ambiguity"
        return (a1 if n else a0).astimezone(UTC), f"ambiguous local time (DST fall-back) resolved by file order as {'second' if n else 'first'} occurrence"
    return a0.astimezone(UTC), None


# ----------------------------------------------------------------------------------------------- interpretation
class _Log:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.skipped = 0
        self.dropped = 0

    def add(self, line: int | None, action: str, reason: str, raw: dict | None = None) -> None:
        if action == "skipped":
            self.skipped += 1
        if len(self.rows) < ROW_LOG_CAP:
            self.rows.append({"line": line, "action": action, "reason": reason, **({"raw": raw} if raw else {})})
        else:
            self.dropped += 1


def _number(raw: str, decimal: str) -> float:
    s = raw.strip().replace("\u00a0", "").replace(" ", "")
    if decimal == ",":
        s = s.replace(".", "").replace(",", ".")
    return float(s)


def interpret(text: str, opt: ImportOptions | None = None) -> ImportResult:
    opt = opt or ImportOptions()
    if len(text.encode()) > MAX_BYTES:
        raise MeterImportError(f"file larger than {MAX_BYTES} bytes")
    tz = _zone(opt.timezone)
    body = text.lstrip("\ufeff")
    delim = _sniff_delimiter(body, opt.delimiter)
    reader = csv.DictReader(io.StringIO(body), delimiter=delim)
    if not reader.fieldnames:
        raise MeterImportError("empty file")
    headers = [h for h in reader.fieldnames if h is not None]
    ts_col, val_col, kind, unit, notes = resolve_columns(headers, opt)
    log = _Log()
    warnings = list(notes)
    factor = 0.001 if unit in ("W", "Wh") else 1.0
    seen_wall: dict[datetime, int] = {}
    raw_rows: list[tuple[datetime, float, str, int]] = []
    total = 0
    for line, row in enumerate(reader, start=2):
        total += 1
        if total > MAX_ROWS:
            log.add(line, "skipped", f"row limit: only the first {MAX_ROWS} data rows are read")
            continue
        ts_raw, v_raw = (row.get(ts_col) or "").strip(), (row.get(val_col) or "").strip()
        if not ts_raw and not v_raw:
            log.add(line, "skipped", "blank row")
            continue
        try:
            ts = _parse_ts(ts_raw, opt.timestamp_format)
        except ValueError:
            log.add(line, "skipped", f"invalid timestamp {ts_raw!r}" + ("" if opt.timestamp_format else " (use ISO-8601 or set timestamp_format)"), {ts_col: ts_raw})
            continue
        if ts.tzinfo is None:
            utc, flag = _localise(ts, tz, seen_wall)
            if utc is None:
                log.add(line, "skipped", flag or "invalid local time", {ts_col: ts_raw})
                continue
            if flag:
                log.add(line, "transformed", flag)
        else:
            utc = ts.astimezone(UTC)
        try:
            v = _number(v_raw, opt.decimal)
        except ValueError:
            log.add(line, "skipped", f"value is not a number ({v_raw!r})", {val_col: v_raw})
            continue
        if not math.isfinite(v):
            log.add(line, "skipped", "value is not finite", {val_col: v_raw})
            continue
        if v < 0 and kind != "cumulative":
            log.add(line, "skipped", "negative value (export/generation is not imported as consumption)", {val_col: v_raw})
            continue
        raw_rows.append((utc, v * factor, ts_raw, line))
    if factor != 1.0 and raw_rows:
        warnings.append(f"values converted from {unit} to {'kW' if UNIT_KIND[unit] == 'power' else 'kWh'} (x0.001)")
    if any(r[2] and _parse_naive(r[2], opt.timestamp_format) for r in raw_rows[:1]):
        warnings.append(f"timestamps without a UTC offset were interpreted as {opt.timezone} local time")
    if not raw_rows:
        raise MeterImportError("no usable rows" + (f": {log.rows[0]['reason']}" if log.rows else ""))

    # order + duplicates
    in_order = sorted(range(len(raw_rows)), key=lambda i: (raw_rows[i][0], i))
    if in_order != list(range(len(raw_rows))):
        moved = sum(1 for a, b in zip(in_order, range(len(raw_rows)), strict=True) if a != b)
        warnings.append(f"{moved} rows were out of time order and were sorted")
        prev = None
        for i in range(len(raw_rows)):
            if prev is not None and raw_rows[i][0] < prev:
                log.add(raw_rows[i][3], "transformed", "out of time order; sorted into place")
            prev = raw_rows[i][0] if prev is None or raw_rows[i][0] > prev else prev
    ordered = [raw_rows[i] for i in in_order]
    dedup: list[tuple[datetime, float, str, int]] = []
    for r in ordered:
        if dedup and r[0] == dedup[-1][0]:
            same = abs(r[1] - dedup[-1][1]) < 1e-12
            log.add(r[3], "skipped", f"duplicate timestamp (first kept at line {dedup[-1][3]}){'' if same else '; values differ'}")
            continue
        dedup.append(r)
    if len(dedup) < len(ordered):
        warnings.append(f"{len(ordered) - len(dedup)} duplicate timestamps were dropped")

    # interval
    diffs = [(b[0] - a[0]).total_seconds() / 60 for a, b in zip(dedup, dedup[1:])]
    interval = opt.interval_minutes or _typical_interval(diffs)
    if interval is None:
        if kind == "cumulative":
            raise MeterImportError("a cumulative meter needs at least two readings")
        raise MeterImportError("a single reading has no interval; set interval_minutes")
    if interval <= 0:
        raise MeterImportError("interval_minutes must be positive")

    readings: list[Reading] = []
    gaps: list[dict[str, Any]] = []
    shift = timedelta(minutes=interval) if opt.timestamp_marks == "end" else timedelta(0)
    if kind in ("power", "interval_energy"):
        for i, (t, v, raw, line) in enumerate(dedup):
            start = t - shift
            kwh = v * interval / 60 if kind == "power" else v
            kw = v if kind == "power" else v / (interval / 60)
            readings.append(Reading(start, interval, kwh, kw, raw, line))
            if i:
                gap = (t - dedup[i - 1][0]).total_seconds() / 60
                if gap > interval * 1.5:
                    gaps.append({"from_utc": (dedup[i - 1][0] - shift + timedelta(minutes=interval)).isoformat(), "to_utc": start.isoformat(),
                                 "missing_intervals": int(round(gap / interval)) - 1, "after_line": dedup[i - 1][3]})
    else:
        for (t0, v0, _r0, _l0), (t1, v1, raw1, line1) in zip(dedup, dedup[1:]):
            minutes = (t1 - t0).total_seconds() / 60
            d = v1 - v0
            flags: list[str] = []
            if d < 0:
                if opt.rollover_kwh and v0 > 0.5 * opt.rollover_kwh and v1 < 0.5 * opt.rollover_kwh:
                    d = v1 + opt.rollover_kwh - v0
                    flags.append("rollover")
                    log.add(line1, "transformed", f"meter rollover at {opt.rollover_kwh} kWh: consumption = {round(d, 6)} kWh")
                else:
                    log.add(line1, "skipped", f"register decreased by {round(-d, 6)} kWh (meter reset or replacement): interval not counted, never negative consumption")
                    gaps.append({"from_utc": t0.isoformat(), "to_utc": t1.isoformat(), "missing_intervals": max(1, int(round(minutes / interval))),
                                 "after_line": _l0, "note": "meter reset/decrease: consumption in this interval is unknown"})
                    continue
            if minutes > interval * 1.5:
                flags.append("spans_gap")
                gaps.append({"from_utc": t0.isoformat(), "to_utc": t1.isoformat(), "missing_intervals": int(round(minutes / interval)) - 1,
                             "after_line": _l0, "note": "energy over the gap is known (register difference) but not its time profile"})
            readings.append(Reading(t0, minutes, d, d / (minutes / 60), raw1, line1, flags))
        log.add(dedup[0][3], "transformed", "first register reading is the starting point (no interval of its own)")
    if gaps:
        warnings.append(f"{len(gaps)} gaps ({sum(g['missing_intervals'] for g in gaps)} missing intervals of {interval:g} min); gaps are reported, not filled")
    if opt.resample_minutes:
        readings, extra = resample(readings, opt.resample_minutes, interval)
        warnings += extra
    if log.dropped:
        warnings.append(f"row log truncated: {log.dropped} more entries not shown")
    return ImportResult(options=asdict(opt) | {"resolved": {"timestamp_column": ts_col, "value_column": val_col, "reading_kind": kind, "unit": unit,
                                                            "delimiter": delim}},
                        columns=headers, readings=readings, rows_total=total, rows_skipped=log.skipped, row_log=log.rows, warnings=warnings,
                        gaps=gaps, interval_minutes=interval)  # fmt: skip


def _typical_interval(diffs: list[float]) -> float | None:
    """The most common spacing (gaps make spacings longer, never shorter); ties -> the smallest."""
    pos = [round(d, 6) for d in diffs if d > 0]
    if not pos:
        return None
    counts: dict[float, int] = {}
    for d in pos:
        counts[d] = counts.get(d, 0) + 1
    top = max(counts.values())
    return min(d for d, n in counts.items() if n == top)


def _parse_naive(raw: str, fmt: str | None) -> bool:
    try:
        return _parse_ts(raw, fmt).tzinfo is None
    except ValueError:
        return False


def resample(readings: list[Reading], minutes: float, native: float) -> tuple[list[Reading], list[str]]:
    """Sum energy into fixed UTC buckets of ``minutes``. Buckets not fully covered are flagged 'partial'."""
    if minutes < native - 1e-9:
        raise MeterImportError(f"resample_minutes ({minutes:g}) cannot be finer than the data interval ({native:g} min)")
    width = timedelta(minutes=minutes)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    buckets: dict[datetime, dict[str, Any]] = {}
    for r in readings:
        k = epoch + width * int((r.start_utc - epoch) / width)
        b = buckets.setdefault(k, {"kwh": 0.0, "covered": 0.0, "first": r})
        b["kwh"] += r.kwh
        b["covered"] += r.interval_minutes
    out = []
    partial = 0
    for k in sorted(buckets):
        b = buckets[k]
        flags = []
        if b["covered"] < minutes - 1e-6:
            flags.append(f"partial:{round(100 * b['covered'] / minutes)}%")
            partial += 1
        out.append(Reading(k, minutes, b["kwh"], b["kwh"] / (minutes / 60), b["first"].original_ts, b["first"].source_row, flags))
    notes = [f"resampled to {minutes:g}-minute buckets"]
    if partial:
        notes.append(f"{partial} resampled buckets are only partly covered by data (flagged 'partial')")
    return out, notes
