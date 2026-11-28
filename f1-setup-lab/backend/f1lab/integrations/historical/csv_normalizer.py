"""CSV normalisation: unit conversion and timestamp parsing (Requirement 5).

Pure functions only. Callers (the CSV importer) are responsible for recording
row numbers and original values of anything these functions reject.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# --- units (Req 5.1-5.3) ----------------------------------------------------

# factor = how many standard units (seconds / kph / metres) one unit is worth
TIME_UNITS: dict[str, float] = {"seconds": 1.0, "milliseconds": 0.001}
SPEED_UNITS: dict[str, float] = {"kph": 1.0, "mph": 1.609344, "m/s": 3.6}
DISTANCE_UNITS: dict[str, float] = {"metres": 1.0, "kilometres": 1000.0, "feet": 0.3048}

SUPPORTED_UNITS: dict[str, tuple[str, ...]] = {
    "time": tuple(TIME_UNITS),
    "speed": tuple(SPEED_UNITS),
    "distance": tuple(DISTANCE_UNITS),
}


class UnsupportedUnitError(ValueError):
    """Raised for a unit outside the supported list; names the supported units (Req 5.4)."""

    def __init__(self, field_name: str, unit: object, supported: tuple[str, ...]):
        self.field = field_name
        self.unit = unit
        self.supported = supported
        super().__init__(f"unsupported {field_name} unit {unit!r}; supported units: {', '.join(supported)}")


def _factor(field_name: str, table: dict[str, float], unit: str) -> float:
    if not isinstance(unit, str) or unit not in table:
        raise UnsupportedUnitError(field_name, unit, tuple(table))
    return table[unit]


def _finite(value: float) -> float:
    v = float(value)
    if not math.isfinite(v):
        raise ValueError(f"value must be finite, got {value!r}")
    return v


def convert_time(value: float, unit: str) -> float:
    """Convert a time in `unit` to seconds."""
    f = _factor("time", TIME_UNITS, unit)
    v = _finite(value)
    return v / 1000.0 if unit == "milliseconds" else v * f


def convert_speed(value: float, unit: str) -> float:
    """Convert a speed in `unit` to kph."""
    return _finite(value) * _factor("speed", SPEED_UNITS, unit)


def convert_distance(value: float, unit: str) -> float:
    """Convert a distance in `unit` to metres."""
    return _finite(value) * _factor("distance", DISTANCE_UNITS, unit)


def invert_time(seconds: float, unit: str) -> float:
    """Exact inverse of `convert_time`."""
    _factor("time", TIME_UNITS, unit)
    v = _finite(seconds)
    return v * 1000.0 if unit == "milliseconds" else v


def invert_speed(kph: float, unit: str) -> float:
    """Exact inverse of `convert_speed`."""
    return _finite(kph) / _factor("speed", SPEED_UNITS, unit)


def invert_distance(metres: float, unit: str) -> float:
    """Exact inverse of `convert_distance`."""
    return _finite(metres) / _factor("distance", DISTANCE_UNITS, unit)


# --- timestamps (Req 5.5-5.8) -----------------------------------------------

EPOCH_MS_THRESHOLD = 1e11
_ASSUMED_DATE = (1970, 1, 1)  # HH:MM:SS.fff carries no date

_TIME_ONLY = re.compile(r"^(\d{1,2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?$")
_DATE_TIME = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?\s*(Z|z|[+-]\d{2}:?\d{2})?$"
)
_ISO_TZ_ONLY = re.compile(r"^(Z|z|[+-]\d{2}:?\d{2})$")


def resolve_timezone(user_tz: str | tzinfo | None) -> tzinfo | None:
    """IANA name (or tzinfo) -> tzinfo. None/blank -> None. Invalid name -> ValueError."""
    if user_tz is None:
        return None
    if isinstance(user_tz, tzinfo):
        return user_tz
    name = str(user_tz).strip()
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ValueError(f"invalid IANA timezone {user_tz!r}") from exc


def _offset(text: str) -> timezone:
    if text in ("Z", "z"):
        return UTC
    sign = -1 if text[0] == "-" else 1
    digits = text[1:].replace(":", "")
    return timezone(sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:])))


def _micro(frac: str | None) -> int:
    return int((frac or "0").ljust(6, "0")[:6])


def _parse_numeric(text: str) -> datetime | None:
    try:
        num = float(text)
    except ValueError:
        return None
    if not math.isfinite(num):
        return None
    seconds = num / 1000.0 if abs(num) >= EPOCH_MS_THRESHOLD else num
    try:
        return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(seconds=seconds)
    except (OverflowError, ValueError):
        return None


def parse_timestamp(
    value: object,
    tz_offset: str | None = None,
    user_tz: str | tzinfo | None = None,
) -> datetime | None:
    """Parse one timestamp value; never raises for bad values (returns None).

    Result is timezone-aware UTC when the value carries an offset, is an epoch
    number, or a timezone is supplied (`user_tz`, or `tz_offset` such as "+02:00").
    An offset inside the value wins over both. Otherwise the result is naive and
    the caller must treat it as relative time (Req 5.7). `HH:MM:SS.fff` is placed
    on 1970-01-01. An invalid `user_tz` name raises ValueError (caller error).
    """
    zone = resolve_timezone(user_tz)
    if tz_offset:
        offset = str(tz_offset).strip()
        if not _ISO_TZ_ONLY.match(offset):
            raise ValueError(f"invalid timezone offset {tz_offset!r}")
        zone = _offset(offset)

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _parse_numeric(repr(value))
    text = str(value).strip()
    if not text:
        return None

    parsed = _parse_numeric(text)
    if parsed is not None:
        return parsed
    if re.fullmatch(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?|[+-]?(inf|infinity|nan)", text, re.I):
        return None  # numeric-looking but not finite / out of range

    local: datetime
    in_value_zone: tzinfo | None = None
    m = _DATE_TIME.match(text)
    if m:
        y, mo, d, h, mi, s, frac, off = m.groups()
        try:
            local = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s), _micro(frac))
        except ValueError:
            return None
        in_value_zone = _offset(off) if off else None
    else:
        m = _TIME_ONLY.match(text)
        if m:
            h, mi, s, frac = m.groups()
            try:
                local = datetime(*_ASSUMED_DATE, int(h), int(mi), int(s), _micro(frac))
            except ValueError:
                return None
        else:
            try:  # remaining ISO 8601 shapes (e.g. 2024-05-18T10:00Z, basic offsets)
                local = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
            except ValueError:
                return None
            if len(text) <= 10:  # bare date, no time of day
                return None
            if local.tzinfo is not None:
                in_value_zone, local = local.tzinfo, local.replace(tzinfo=None)

    effective = in_value_zone or zone  # offset in the value takes precedence (Req 5.6)
    if effective is None:
        return local
    return local.replace(tzinfo=effective).astimezone(UTC)


@dataclass
class SessionTimes:
    """Result of `to_session_time`."""

    session_time_s: list[float | None]  # same length as input; None for unparseable rows
    timezone: str | None  # IANA name / "UTC" when known, else None (see `unavailable`)
    unavailable: list[str] = field(default_factory=list)  # contains "timezone" when marked unavailable
    bad_rows: list[tuple[int, object]] = field(default_factory=list)  # (1-based row, original value)
    absolute: bool = False  # True when values are UTC-anchored


def to_session_time(
    values: list[object],
    tz_offset: str | None = None,
    user_tz: str | tzinfo | None = None,
) -> SessionTimes:
    """Convert raw timestamp cells to seconds from the earliest valid timestamp (= 0).

    With no offset in the data and no `user_tz`, the timezone is recorded as an
    Unavailable_Marker (Req 5.7). Otherwise values are UTC-anchored (Req 5.6)
    and the relative origin is still the earliest timestamp.
    """
    zone = resolve_timezone(user_tz)
    parsed: list[datetime | None] = []
    bad: list[tuple[int, object]] = []
    for i, raw in enumerate(values, start=1):
        dt = parse_timestamp(raw, tz_offset, zone)
        parsed.append(dt)
        if dt is None:
            bad.append((i, raw))

    valid = [p for p in parsed if p is not None]
    naive_present = any(p.tzinfo is None for p in valid)
    unavailable: list[str] = []
    tz_name: str | None
    if naive_present:
        tz_name = None
        unavailable.append("timezone")
    else:
        tz_name = getattr(zone, "key", None) or ("UTC" if valid else None)
        if not valid:
            unavailable.append("timezone")

    def key(p: datetime) -> datetime:
        return p.replace(tzinfo=None) if p.tzinfo is not None else p  # aware values are already UTC

    origin = min((key(p) for p in valid), default=None)
    rel = [None if p is None else (key(p) - origin).total_seconds() for p in parsed]
    return SessionTimes(rel, tz_name, unavailable, bad, absolute=bool(valid) and not naive_present)


# =============================================================================
# Lap assignment, canonical frame builders, manifest and the shared entry point
# (Requirements 2.3, 2.6, 2.7, 3.4, 3.5, 3.7, 5.9, 5.10, 7.5, 8.3, 14.2)
# =============================================================================
import io  # noqa: E402
from typing import Any  # noqa: E402

import pandas as pd  # noqa: E402

from db.models.f1 import LAP_COLUMNS, TELEMETRY_COLUMNS  # noqa: E402

PROVIDER = "csv_upload"
PROVIDER_LABEL = "User CSV"
SOURCE_TYPE = "user_uploaded"
VERIFICATION = "unverified"
DATA_TYPE_FULL = "Full telemetry"
DATA_TYPE_LAPS = "Lap summaries"
LAP_SUMMARY = "lap_summary"
SAMPLED_TELEMETRY = "sampled_telemetry"

DUPLICATE_POLICIES = ("drop_exact_duplicates", "reject")
ORDER_POLICIES = ("sort", "reject")

_UNIT_ALIASES = {"s": "seconds", "sec": "seconds", "ms": "milliseconds", "m": "metres", "km": "kilometres",
                 "ft": "feet", "meters": "metres", "kilometers": "kilometres"}
_PLAIN_CHANNELS = ("throttle_pct", "brake_pct", "steer_pct", "gear", "rpm", "drs", "brake_applied")
_LAP_ALIASES = {"lap": "lap_number"}
_SECTOR_FIELDS = ("lap_time_s", "sector_1_s", "sector_2_s", "sector_3_s", "sector_4_s")


def _unit(name: object) -> object:
    return _UNIT_ALIASES.get(name, name) if isinstance(name, str) else name


def normalize_units(units: dict | None) -> dict[str, str]:
    """Resolve unit aliases (``s``, ``ms``, ``m``, ...) and defaults; raises UnsupportedUnitError for unknown units."""
    u = dict(units or {})
    out = {
        "time": _unit(u.get("time", u.get("time_unit", "seconds"))),
        "speed": _unit(u.get("speed", u.get("speed_unit", "kph"))),
        "distance": _unit(u.get("distance", u.get("distance_unit", "metres"))),
    }
    _factor("time", TIME_UNITS, out["time"])
    _factor("speed", SPEED_UNITS, out["speed"])
    _factor("distance", DISTANCE_UNITS, out["distance"])
    return out  # type: ignore[return-value]


def _cell_num(cell: object) -> float | None:
    """A finite float from a cell, or None for empty / non-numeric / non-finite cells."""
    if cell is None or isinstance(cell, bool):
        return None
    try:
        v = float(str(cell).strip().replace("\u00a0", "")) if not isinstance(cell, (int, float)) else float(cell)
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def _is_blank(cell: object) -> bool:
    return cell is None or (isinstance(cell, float) and math.isnan(cell)) or str(cell).strip() == ""


def _as_frame(rows: Any) -> pd.DataFrame:
    if isinstance(rows, pd.DataFrame):
        return rows.reset_index(drop=True)
    return pd.DataFrame(list(rows or []))


def _canonical_mapping(mapping: dict | None) -> dict[str, str]:
    """{standard_field: csv_column}; blank entries dropped and the `lap` alias folded into `lap_number`."""
    out: dict[str, str] = {}
    for k, v in (mapping or {}).items():
        if v is None or str(v).strip() == "":
            continue
        out[_LAP_ALIASES.get(k, k)] = str(v).strip()
    return out


def _col(df: pd.DataFrame, name: str | None) -> list[object] | None:
    if not name:
        return None
    lookup = {str(c).strip(): c for c in df.columns}
    key = lookup.get(name)
    return None if key is None else df[key].tolist()


def _issue(issues: list[dict] | None, row: int, column: str, value: object, reason: str, severity: str = "warning") -> None:
    if issues is not None:
        issues.append({"row": row, "column": column, "value": value, "reason": reason, "severity": severity})


def assign_laps(frame: pd.DataFrame, lap_mapping: str | None, lap_summary_frame: pd.DataFrame | None = None,
                invalid: list[tuple[int, object]] | None = None) -> pd.Series:
    """Lap number per sample row (nullable Int64; <NA> is the Unavailable_Marker).

    Laps are never inferred (Req 5.9): with no mapped `lap_number` column every row is unavailable, even when a
    companion lap-summary frame is supplied, because a lap summary carries no sample timing to infer from.
    A mapped value that is an integer >= 1 is assigned verbatim; anything else is appended to `invalid` as
    (1-based row, original value) and that row stays unavailable (Req 5.10).
    """
    frame = _as_frame(frame)
    n = len(frame)
    cells = _col(frame, lap_mapping)
    if cells is None:
        return pd.Series(pd.array([pd.NA] * n, dtype="Int64"), index=range(n))
    out: list[Any] = []
    for i, cell in enumerate(cells, start=1):
        v = _cell_num(cell)
        if v is not None and v == int(v) and v >= 1:
            out.append(int(v))
        else:
            out.append(pd.NA)
            if invalid is not None:
                invalid.append((i, cell))
    return pd.Series(pd.array(out, dtype="Int64"), index=range(n))


def _convert_cells(cells: list[object] | None, n: int, converter, unit: str | None, column: str,
                   issues: list[dict] | None, positive: bool = False) -> list[float]:
    """Numeric cells -> floats (NaN = missing). Bad cells become NaN and are reported; never interpolated."""
    if cells is None:
        return [math.nan] * n
    out: list[float] = []
    for i, cell in enumerate(cells, start=1):
        v = _cell_num(cell)
        if v is None or (positive and v <= 0):
            reason = "empty" if _is_blank(cell) else ("non_positive" if v is not None else "non_numeric")
            _issue(issues, i, column, cell, reason)
            out.append(math.nan)
        else:
            out.append(converter(v, unit) if converter else v)
    return out


def build_lap_frame(rows: Any, mapping: dict, units: dict | None = None, issues: list[dict] | None = None) -> pd.DataFrame:
    """Lap rows -> a frame with exactly `LAP_COLUMNS`. Unmapped fields are null; `lap_time_s` is never the sector sum."""
    df = _as_frame(rows)
    m = _canonical_mapping(mapping)
    u = normalize_units(units)
    n = len(df)
    data: dict[str, Any] = {c: pd.Series([None] * n, dtype=object) for c in LAP_COLUMNS}
    data["lap_number"] = assign_laps(df, m.get("lap_number"), invalid=None)
    if m.get("lap_number"):
        bad: list[tuple[int, object]] = []
        assign_laps(df, m["lap_number"], invalid=bad)
        for r, v in bad:
            _issue(issues, r, m["lap_number"], v, "invalid_lap_number")
    for fld in _SECTOR_FIELDS:
        if fld in m:
            vals = _convert_cells(_col(df, m[fld]), n, convert_time, u["time"], m[fld], issues, positive=True)
            data[fld] = pd.Series(vals, dtype="float64")
        else:
            data[fld] = pd.Series([math.nan] * n, dtype="float64")
    if "driver" in m:
        cells = _col(df, m["driver"]) or []
        data["driver"] = pd.Series([None if _is_blank(c) else str(c).strip() for c in cells], dtype=object)
    frame = pd.DataFrame({c: data[c] for c in LAP_COLUMNS})
    frame.attrs["unavailable"] = [c for c in LAP_COLUMNS if frame[c].isna().all()]
    return frame


def _empty_telemetry() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series([], dtype="Int64" if c == "lap_number" else "float64") for c in TELEMETRY_COLUMNS})


def build_telemetry_frame(rows: Any, mapping: dict, units: dict | None = None, *, tz_offset: str | None = None,
                          user_tz: str | tzinfo | None = None, issues: list[dict] | None = None,
                          duplicate_policy: str | None = None, order_policy: str | None = None) -> pd.DataFrame:
    """Sample rows -> a frame with exactly `TELEMETRY_COLUMNS`.

    Unmapped channels are all-null (never written). Empty / non-numeric mapped cells are null, never interpolated.
    Rows without a usable time value are excluded and reported (Req 5.8, 3.9). Result `attrs`: `unavailable`
    (unmapped channels), `timezone`, `timezone_unavailable`, `skipped_rows`, `absolute_time`, `duplicates_dropped`,
    `out_of_order`, `time_error`. Time is `session_time_s` if mapped, else `timestamp`.
    """
    df = _as_frame(rows)
    m = _canonical_mapping(mapping)
    u = normalize_units(units)
    n = len(df)
    attrs: dict[str, Any] = {"timezone": None, "timezone_unavailable": False, "skipped_rows": 0, "absolute_time": False,
                             "duplicates_dropped": 0, "out_of_order": 0, "time_error": None}

    def fail(msg: str) -> pd.DataFrame:
        attrs["time_error"] = msg
        _issue(issues, 0, "timestamp", None, msg, "error")
        out = _empty_telemetry()
        out.attrs.update(attrs, unavailable=list(TELEMETRY_COLUMNS))
        return out

    # ---- time (Req 3.6, 5.5-5.8)
    time_vals: list[float | None]
    if "session_time_s" in m:
        cells = _col(df, m["session_time_s"])
        if cells is None:
            return fail(f"time column {m['session_time_s']!r} not found")
        time_vals = []
        for i, c in enumerate(cells, start=1):
            v = _cell_num(c)
            if v is None:
                _issue(issues, i, m["session_time_s"], c, "unparseable_time")
            time_vals.append(None if v is None else convert_time(v, u["time"]))
    elif "timestamp" in m:
        cells = _col(df, m["timestamp"])
        if cells is None:
            return fail(f"time column {m['timestamp']!r} not found")
        st = to_session_time(cells, tz_offset, user_tz)
        for r, v in st.bad_rows:
            _issue(issues, r, m["timestamp"], v, "unparseable_time")
        time_vals = list(st.session_time_s)
        attrs.update(timezone=st.timezone, timezone_unavailable="timezone" in st.unavailable, absolute_time=st.absolute)
    else:
        return fail("no time column mapped (timestamp or session_time_s)")
    if not any(t is not None for t in time_vals):
        return fail("time column has no usable values")

    # ---- channels (Req 3.4, 3.5), converted before any row is dropped so row numbers stay original
    lap_invalid: list[tuple[int, object]] = []
    laps = assign_laps(df, m.get("lap_number"), invalid=lap_invalid)
    for r, v in lap_invalid:
        _issue(issues, r, m["lap_number"], v, "invalid_lap_number")
    cols: dict[str, list] = {"lap_number": laps.tolist()}
    cols["session_time_s"] = [math.nan if t is None else t for t in time_vals]
    cols["distance_m"] = _convert_cells(_col(df, m.get("distance_m")), n, convert_distance, u["distance"], m.get("distance_m", ""), issues)
    cols["speed_kph"] = _convert_cells(_col(df, m.get("speed_kph")), n, convert_speed, u["speed"], m.get("speed_kph", ""), issues)
    for ch in _PLAIN_CHANNELS:
        cols[ch] = _convert_cells(_col(df, m.get(ch)), n, None, None, m.get(ch, ""), issues)
    unavailable = [c for c in TELEMETRY_COLUMNS if c != "session_time_s" and c not in m]
    for c in ("lap_number", "distance_m", "speed_kph", *_PLAIN_CHANNELS):
        if c in m and _col(df, m[c]) is None:
            unavailable.append(c)
    frame = pd.DataFrame({c: pd.Series(cols[c], dtype="Int64" if c == "lap_number" else "float64") for c in TELEMETRY_COLUMNS})

    # ---- exact duplicates over every mapped column (Req 6.2/6.7/6.8)
    if duplicate_policy:
        mapped_cols = [df.columns[[str(c).strip() for c in df.columns].index(v)] for v in dict.fromkeys(m.values())
                       if v in [str(c).strip() for c in df.columns]]
        dup = df.duplicated(subset=mapped_cols, keep="first").to_numpy() if mapped_cols else [False] * n
        dups = [i for i, d in enumerate(dup) if d]
        if dups and duplicate_policy == "reject":
            for i in dups:
                _issue(issues, i + 1, "*", None, "duplicate_row", "error")
        elif dups:
            attrs["duplicates_dropped"] = len(dups)
            keep = ~pd.Series(dup)
            frame, time_vals = frame[keep.to_numpy()], [t for t, k in zip(time_vals, keep) if k]

    # ---- rows without usable time are excluded, never given a generated timestamp
    usable = frame["session_time_s"].notna().to_numpy()
    attrs["skipped_rows"] = int((~usable).sum())
    frame = frame[usable]

    # ---- ordering (Req 6.3/6.9/6.10)
    t = frame["session_time_s"].to_numpy()
    out_of_order = int((t[1:] < t[:-1]).sum()) if len(t) > 1 else 0
    attrs["out_of_order"] = out_of_order
    if out_of_order:
        if order_policy == "sort":
            frame = frame.sort_values("session_time_s", kind="stable")
        elif order_policy == "reject":
            _issue(issues, 0, "session_time_s", None, "out_of_order", "error")
    frame = frame.reset_index(drop=True)
    frame.attrs.update(attrs, unavailable=unavailable)
    return frame


def build_manifest(session_metadata: dict, mapping: dict, units: dict, file_info: dict, data_type_label: str,
                   *, session_id: str | None = None, drivers: list | None = None, channels: dict | None = None) -> dict:
    """Manifest for a CSV session, shaped like `openf1_normalizer.build_manifest` plus CSV provenance.

    `metadata` holds all 11 Session_Metadata items; an item that is None is only valid when listed in
    `unavailable` (the Unavailable_Marker). Nothing is defaulted or guessed (Req 7.5).
    """
    meta_in = dict(session_metadata or {})
    unavailable = list(meta_in.get("unavailable") or [])
    u = normalize_units(units)
    file_hash, filename = file_info.get("file_hash"), file_info.get("original_filename")
    metadata = {
        "provider": PROVIDER_LABEL, "year": meta_in.get("year"), "event": meta_in.get("event"),
        "circuit": meta_in.get("circuit"), "session_type": meta_in.get("session_type"), "driver": meta_in.get("driver"),
        "session_date": meta_in.get("session_date"), "timezone": meta_in.get("timezone"),
        "original_filename": filename, "time_unit": u["time"], "distance_unit": u["distance"],
        "unavailable": unavailable,
    }
    if data_type_label not in (DATA_TYPE_FULL, DATA_TYPE_LAPS):
        raise ValueError(f"unknown data type label {data_type_label!r}")
    if session_id is None and metadata["year"] is not None and metadata["event"] and metadata["session_type"]:
        from f1lab.integrations.historical.repository import session_id as make_session_id
        session_id = make_session_id(int(metadata["year"]), metadata["event"], metadata["session_type"], PROVIDER)
    return {
        "provider": PROVIDER, "source_type": SOURCE_TYPE, "verification": VERIFICATION,
        "source_label": PROVIDER_LABEL, "session_id": session_id, "year": metadata["year"], "event": metadata["event"],
        "circuit": metadata["circuit"], "session": metadata["session_type"], "session_type": metadata["session_type"],
        "session_date": metadata["session_date"], "imported_at": datetime.now(UTC).isoformat(),
        "drivers": list(drivers or []), "channels": dict(channels or {}), "import_status": "COMPLETED",
        "data_type": data_type_label, "column_mapping": dict(mapping or {}), "units": {"time": u["time"], "speed": u["speed"],
                                                                                         "distance": u["distance"]},
        "file_hash": file_hash, "original_filename": filename, "metadata": metadata,
    }


@dataclass
class ParsedCsv:
    """Result of `parse_csv`; shared by the CSV Upload provider and the Setup Engineering adapter."""

    kind: str  # LAP_SUMMARY | SAMPLED_TELEMETRY
    data_type_label: str
    laps: pd.DataFrame
    telemetry: pd.DataFrame
    issues: list[dict] = field(default_factory=list)
    blocking_errors: list[dict] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)  # unmapped standard fields (never populated)
    timezone: str | None = None
    timezone_unavailable: bool = False
    skipped_rows: int = 0
    duplicates_dropped: int = 0
    out_of_order: int = 0
    row_count: int = 0


def parse_csv(text: str, mapping: dict, units: dict | None = None, policy: dict | None = None) -> ParsedCsv:
    """Single entry point: CSV text + Column_Mapping + units + policy -> normalized frames.

    `mapping` is {standard_field: csv_column}. `policy` keys (all optional): `data_type` (`lap_summary` |
    `sampled_telemetry`; default: telemetry when a time field is mapped, else lap summary), `duplicate_policy`,
    `order_policy`, `tz_offset`, `timezone`. A Lap_Summary yields no telemetry rows (Req 2.6).
    """
    policy = dict(policy or {})
    u = normalize_units(units)
    m = _canonical_mapping(mapping)
    df = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False, skipinitialspace=False)
    df.columns = [str(c).strip() for c in df.columns]
    kind = policy.get("data_type") or (SAMPLED_TELEMETRY if ("timestamp" in m or "session_time_s" in m) else LAP_SUMMARY)
    if kind not in (LAP_SUMMARY, SAMPLED_TELEMETRY):
        raise ValueError(f"unknown data_type {kind!r}")
    dup, order = policy.get("duplicate_policy"), policy.get("order_policy")
    for name, val, allowed in (("duplicate_policy", dup, DUPLICATE_POLICIES), ("order_policy", order, ORDER_POLICIES)):
        if val is not None and val not in allowed:
            raise ValueError(f"unknown {name} {val!r}; supported: {', '.join(allowed)}")

    issues: list[dict] = []
    missing = [c for c in dict.fromkeys(m.values()) if c not in set(df.columns)]
    for c in missing:
        _issue(issues, 0, c, None, "mapped_column_missing", "error")
    if missing:
        laps, tel = pd.DataFrame({c: [] for c in LAP_COLUMNS}), _empty_telemetry()
        attrs: dict[str, Any] = {}
    elif kind == LAP_SUMMARY:
        laps, tel, attrs = build_lap_frame(df, m, u, issues), _empty_telemetry(), {}
    else:
        tel = build_telemetry_frame(df, m, u, tz_offset=policy.get("tz_offset"), user_tz=policy.get("timezone"),
                                    issues=issues, duplicate_policy=dup, order_policy=order)
        laps, attrs = pd.DataFrame({c: [] for c in LAP_COLUMNS}), tel.attrs
    frame = laps if kind == LAP_SUMMARY else tel
    return ParsedCsv(
        kind=kind, data_type_label=DATA_TYPE_LAPS if kind == LAP_SUMMARY else DATA_TYPE_FULL, laps=laps, telemetry=tel,
        issues=issues, blocking_errors=[i for i in issues if i["severity"] == "error"],
        unavailable=list(frame.attrs.get("unavailable", [])), timezone=attrs.get("timezone"),
        timezone_unavailable=bool(attrs.get("timezone_unavailable")), skipped_rows=int(attrs.get("skipped_rows", 0)),
        duplicates_dropped=int(attrs.get("duplicates_dropped", 0)), out_of_order=int(attrs.get("out_of_order", 0)),
        row_count=len(df),
    )
