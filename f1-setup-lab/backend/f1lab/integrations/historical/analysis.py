"""Pure analysis helpers for historical Phase 1 features.

No database, network or FastAPI imports. Only numpy, pandas and the stdlib.
"""
from __future__ import annotations

import csv
import io
import math
import re
from urllib.parse import urlsplit

import numpy as np
import pandas as pd

# Local copy so this module stays pure; a test keeps it equal to f1lab.HISTORICAL_NOTICE.
HISTORICAL_NOTICE = (
    "HISTORICAL F1 DATA: real Formula 1 session data from OpenF1 (unofficial, not affiliated with Formula 1). "
    "Used for analysis only; some values are estimated from it, and the import coverage on each session says which."
)


_COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
_HEX6 = re.compile(r"[0-9A-Fa-f]{6}")
_FORMULA_PREFIXES = ("=", "+", "-", "@")
_MAX_URL_LEN = 500


def compass(deg: float) -> str:
    """Return the 8-point compass label for a bearing in degrees (D6)."""
    return _COMPASS[int(((deg % 360) + 22.5) // 45) % 8]


def clean_colour(value: object) -> str | None:
    """Return an upper-case six-hex-digit colour, or None if invalid."""
    try:
        if not isinstance(value, str):
            return None
        text = value.strip()
        if _HEX6.fullmatch(text) is None:
            return None
        return text.upper()
    except Exception:
        return None


def clean_url(value: object) -> str | None:
    """Return the URL if it is https with a host and <= 500 chars, else None."""
    try:
        if not isinstance(value, str):
            return None
        text = value.strip()
        if not text or len(text) > _MAX_URL_LEN:
            return None
        if any(ch.isspace() or ord(ch) < 32 for ch in text):
            return None
        parts = urlsplit(text)
        if parts.scheme.lower() != "https" or not parts.hostname:
            return None
        return text
    except Exception:
        return None


def _csv_cell(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def csv_text(header: list, rows: list) -> str:
    """Build RFC 4180 CSV text with formula-injection protection (D10)."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow([_csv_cell(h) for h in header])
    for row in rows:
        writer.writerow([_csv_cell(c) for c in row])
    return buf.getvalue()


DISTANCE_NOTE = (
    "distance is an estimate (speed integrated within each lap); "
    "laps are compared on normalised distance"
)
_MIN_INCREASING_SHARE = 0.9


def _clean_lap(telemetry: object) -> tuple[np.ndarray, np.ndarray]:
    """Return strictly increasing (distance, time) arrays or raise Conflict."""
    from f1lab.errors import Conflict

    frame = telemetry if isinstance(telemetry, pd.DataFrame) else pd.DataFrame(telemetry)
    cols = {}
    for channel in ("distance_m", "session_time_s"):
        if channel not in frame.columns:
            raise Conflict(f"Telemetry channel {channel} is missing.")
        cols[channel] = pd.to_numeric(frame[channel], errors="coerce").to_numpy(dtype=float)
    dist, tim = cols["distance_m"], cols["session_time_s"]
    keep = ~(np.isnan(dist) | np.isnan(tim))
    for channel, arr in (("distance_m", dist), ("session_time_s", tim)):
        if not keep.any() or len(arr) < 2:
            raise Conflict(f"Telemetry channel {channel} has too few samples.")
    dist, tim = dist[keep], tim[keep]
    n = len(dist)
    if n < 2:
        raise Conflict("Telemetry channel distance_m has too few samples.")
    for channel, arr in (("distance_m", dist), ("session_time_s", tim)):
        share = (1 + int(np.count_nonzero(np.diff(arr) > 0))) / n
        if share < _MIN_INCREASING_SHARE:
            raise Conflict(
                f"Telemetry channel {channel} is not strictly increasing "
                f"over at least 90 percent of samples."
            )
    # Drop samples that do not advance either channel past the running best.
    sel = [0]
    for i in range(1, n):
        if dist[i] > dist[sel[-1]] and tim[i] > tim[sel[-1]]:
            sel.append(i)
    if len(sel) < 2 or len(sel) / n < _MIN_INCREASING_SHARE:
        raise Conflict(
            "Telemetry channels distance_m and session_time_s are not "
            "strictly increasing over at least 90 percent of samples."
        )
    return dist[sel], tim[sel]


def _slice_times(telemetry: object, count: int) -> tuple[np.ndarray, float]:
    dist, tim = _clean_lap(telemetry)
    bounds = dist[0] + np.arange(count + 1) * (dist[-1] - dist[0]) / count
    bounds[-1] = dist[-1]
    t = np.interp(bounds, dist, tim)
    return np.diff(t), float(tim[-1] - tim[0])


def mini_sectors(telemetry: object, count: int, ref: object = None) -> dict:
    """Split a lap into `count` equal-distance slices (D7).

    `telemetry` and `ref` are frames/dicts with `distance_m` and `session_time_s`.
    With a reference, laps are compared slice by slice on normalised distance;
    delta is driver minus reference (positive means slower).
    Raises Conflict naming the failing channel for unusable telemetry.
    """
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise ValueError("count must be a positive integer")
    times, lap_time = _slice_times(telemetry, count)
    ref_times = None
    if ref is not None:
        ref_times, _ = _slice_times(ref, count)
    slices = []
    cum = 0.0
    for i in range(count):
        item = {
            "index": i,
            "start_frac": i / count,
            "end_frac": (i + 1) / count,
            "time_s": float(times[i]),
        }
        if ref_times is not None:
            delta = float(times[i] - ref_times[i])
            cum += delta
            item["ref_time_s"] = float(ref_times[i])
            item["delta_s"] = delta
            item["cum_delta_s"] = cum
        slices.append(item)
    return {
        "count": count,
        "distance_note": DISTANCE_NOTE,
        "lap_time_s": lap_time,
        "sum_slices_s": float(times.sum()),
        "slices": slices,
    }


# ---------------------------------------------------------------------------
# Tyre degradation (D8)
# ---------------------------------------------------------------------------
MIN_DEGRADATION_LAPS = 5
FEW_LAPS_REASON = "fewer than 5 usable laps"
FUEL_CAVEAT = (
    "Fuel burn is not corrected; a negative slope can be caused by falling fuel load. These slopes are observed lap-time trends, "
    "not a fuel-corrected tyre-wear model: fuel burn, track evolution, safety cars, traffic and driver management also change lap "
    "times, so a negative slope does not show that the tyres improve with age."
)
_SC_START = ("SAFETY CAR DEPLOYED", "VIRTUAL SAFETY CAR DEPLOYED", "VSC DEPLOYED")
_SC_END = ("SAFETY CAR IN THIS LAP", "VIRTUAL SAFETY CAR ENDING", "VSC ENDING")


def _records(data: object) -> list[dict]:
    if data is None:
        return []
    if isinstance(data, pd.DataFrame):
        return data.to_dict("records")
    return [dict(r) for r in data]


def _num(value: object) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) else out


def _is_nan(value: object) -> bool:
    return isinstance(value, float) and math.isnan(value)


def _flag(value: object) -> bool:
    if value is None:
        return False
    try:
        if isinstance(value, float) and math.isnan(value):
            return False
    except Exception:
        pass
    return bool(value)


def _safety_car_windows(race_control: object) -> list[tuple[float, float]]:
    """Return inclusive (first_lap, last_lap) windows from race control text."""
    rows = []
    for r in _records(race_control):
        lap = _num(r.get("lap_number"))
        text = str(r.get("message") or "").upper()
        if lap is None or not text:
            continue
        rows.append((lap, text))
    rows.sort(key=lambda x: x[0])  # stable: keeps message order within a lap
    windows: list[tuple[float, float]] = []
    start = None
    for lap, text in rows:
        if any(s in text for s in _SC_START):
            if start is None:
                start = lap
        elif any(e in text for e in _SC_END) and start is not None:
            windows.append((start, lap))
            start = None
    if start is not None:
        windows.append((start, math.inf))
    return windows


def _fit(xs: list[float], ys: list[float]) -> tuple[float, float | None]:
    x = np.asarray(xs, dtype=float)
    y = np.asarray(ys, dtype=float)
    if float(np.ptp(x)) == 0.0:
        return None, None  # type: ignore[return-value]
    slope, intercept = np.polyfit(x, y, 1)
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    if ss_tot == 0:
        return float(slope), None
    ss_res = float(np.sum((y - (slope * x + intercept)) ** 2))
    return float(slope), 1.0 - ss_res / ss_tot


def tyre_degradation(laps: object, race_control: object = None) -> dict:
    """Per driver and stint tyre degradation slope (D8, Requirements 6.1 to 6.5)."""
    rows = _records(laps)
    windows = _safety_car_windows(race_control)
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        stint = _num(r.get("stint"))
        driver = r.get("driver")
        if stint is None or driver is None:
            continue
        groups.setdefault((str(driver), int(stint)), []).append(r)

    driver_first_stint: dict[str, int] = {}
    pit_laps: dict[str, set] = {}
    for (drv, stint), items in groups.items():
        driver_first_stint[drv] = min(stint, driver_first_stint.get(drv, stint))
        for r in items:
            ln = _num(r.get("lap_number"))
            if ln is not None and _flag(r.get("pits")):
                pit_laps.setdefault(drv, set()).add(ln)

    stints = []
    for (drv, stint) in sorted(groups):
        items = sorted(groups[(drv, stint)], key=lambda r: _num(r.get("lap_number")) or 0)
        nums = [_num(r.get("lap_number")) for r in items if _num(r.get("lap_number")) is not None]
        first_lap = nums[0] if nums else None
        follows_pit = first_lap is not None and (
            stint > driver_first_stint[drv] or (first_lap - 1) in pit_laps.get(drv, set())
        )
        excluded = {"pit": 0, "invalid": 0, "deleted": 0, "out_lap": 0, "safety_car": 0}
        xs: list[float] = []
        ys: list[float] = []
        compounds = [str(r["compound"]) for r in items if r.get("compound") not in (None, "") and not (isinstance(r.get("compound"), float) and math.isnan(r["compound"]))]
        ages = [a for a in (_num(r.get("tyre_life")) for r in items) if a is not None]
        for r in items:
            ln = _num(r.get("lap_number"))
            t = _num(r.get("lap_time_s"))
            age = _num(r.get("tyre_life"))
            if _flag(r.get("is_deleted")):
                excluded["deleted"] += 1
            elif not _flag(r.get("is_valid")) or t is None or age is None:
                excluded["invalid"] += 1
            elif _flag(r.get("pits")):
                excluded["pit"] += 1
            elif follows_pit and ln is not None and ln == first_lap:
                excluded["out_lap"] += 1
            elif ln is not None and any(a <= ln <= b for a, b in windows):
                excluded["safety_car"] += 1
            else:
                xs.append(age)
                ys.append(t)
        slope = r2 = None
        reason = None
        if len(xs) < MIN_DEGRADATION_LAPS:
            reason = FEW_LAPS_REASON
        else:
            slope, r2 = _fit(xs, ys)
            if slope is None:
                reason = "tyre age does not vary"
        stints.append({
            "driver": drv,
            "stint": stint,
            "compound": compounds[0] if compounds else None,
            "tyre_age_first": int(min(ages)) if ages else None,
            "tyre_age_last": int(max(ages)) if ages else None,
            "laps_used": len(xs),
            "laps_excluded": excluded,
            "slope_s_per_lap": slope,
            "r_squared": r2,
            "reason": reason,
        })

    by_comp: dict[str, list[float]] = {}
    for s in stints:
        if s["slope_s_per_lap"] is not None and s["compound"] is not None:
            by_comp.setdefault(s["compound"], []).append(s["slope_s_per_lap"])
    by_compound = [
        {"compound": c, "median_slope_s_per_lap": float(np.median(v)), "stints": len(v)}
        for c, v in sorted(by_comp.items())
    ]
    return {"stints": stints, "by_compound": by_compound, "caveat": FUEL_CAVEAT, "coverage_note": None}


# ---------------------------------------------------------------------------
# Season comparison (D9)
# ---------------------------------------------------------------------------
COMPARE_METRICS = ("best_lap", "median_lap", "finish_position", "grid_position", "points")
_DATASET_FOR_METRIC = {
    "finish_position": "session_result",
    "grid_position": "starting_grid",
    "points": "drivers_championship",
}
NOT_IN_SESSION = "driver not in session"
NO_VALID_LAPS = "no valid laps"


def _session_label(s: dict) -> str:
    parts = [s.get("year"), s.get("event"), s.get("session_type")]
    return " ".join(str(p) for p in parts if p not in (None, ""))


def _session_driver_numbers(s: dict) -> dict:
    """Map driver code to number for one session (None when the number is unknown)."""
    out: dict = {}
    for d in _records(s.get("drivers")):
        code = d.get("code")
        if code not in (None, ""):
            out[str(code)] = d.get("number")
    return out


def _compare_cell(session: dict, code: str, metric: str, numbers: dict) -> tuple[float | None, str | None]:
    if metric in ("best_lap", "median_lap"):
        laps = _records(session.get("laps"))
        mine = [r for r in laps if str(r.get("driver")) == code]
        if code not in numbers and not mine:
            return None, NOT_IN_SESSION
        # OpenF1 may publish no validity at all: when no lap in the session has a value, do not filter on it
        has_validity = any(r.get("is_valid") is not None and not _is_nan(r.get("is_valid")) for r in laps)
        times = []
        for r in mine:
            t = _num(r.get("lap_time_s"))
            if t is None or (has_validity and not _flag(r.get("is_valid"))) or _flag(r.get("is_deleted")) or _flag(r.get("pits")):
                continue
            times.append(t)
        if not times:
            return None, NO_VALID_LAPS
        return (min(times) if metric == "best_lap" else float(np.median(times))), None

    name = _DATASET_FOR_METRIC[metric]
    rows = (session.get("datasets") or {}).get(name)
    rows = _records(rows)
    if not rows:
        return None, f"{name} not stored"
    row = next((r for r in rows if str(r.get("driver")) == code), None)
    if row is None:
        return None, NOT_IN_SESSION
    if metric == "points":
        start, cur = _num(row.get("points_start")), _num(row.get("points_current"))
        if start is None or cur is None:
            return None, "points not recorded for driver"
        return cur - start, None
    pos = _num(row.get("position"))
    if pos is None:
        return None, f"no {metric.replace('_', ' ')} recorded for driver"
    return int(pos), None


def season_compare(sessions: list, drivers: list, metric: str) -> dict:
    """Compare drivers across sessions (D9, Requirements 4.1 to 4.3, 4.5, 4.6).

    Each session is a dict with `id`, `year`, `event`, `session_type`,
    `date_start`, `drivers` (list of {code, number}), `laps` (records with
    driver code, lap_time_s, is_valid, is_deleted, pits) and `datasets`
    ({name: list of records, or None when not stored}).
    Returns one cell per driver per session, sessions ordered by date_start.
    A missing value is None with a reason string, never 0.
    """
    if metric not in COMPARE_METRICS:
        raise ValueError(f"metric must be one of: {', '.join(COMPARE_METRICS)}")
    ordered = sorted(
        enumerate(sessions), key=lambda p: (str(p[1].get("date_start") or ""), p[0])
    )
    codes = [str(d) for d in drivers]
    out_sessions = []
    cells = []
    for _, s in ordered:
        numbers = _session_driver_numbers(s)
        out_sessions.append({
            "id": s.get("id"),
            "label": _session_label(s),
            "year": s.get("year"),
            "event": s.get("event"),
            "session_type": s.get("session_type"),
            "date_start": s.get("date_start"),
            "driver_numbers": {c: numbers.get(c) for c in codes},
        })
        for code in codes:
            value, reason = _compare_cell(s, code, metric, numbers)
            cells.append({"session_id": s.get("id"), "driver": code, "value": value, "reason": reason})
    return {"metric": metric, "drivers": codes, "sessions": out_sessions, "cells": cells}


# ---------------------------------------------------------------------------
# Report helpers: finishing gaps, observed trend wording, weather sensor dropouts
# ---------------------------------------------------------------------------
def gap_display(row: dict) -> str:
    """Finishing gap for the report. OpenF1 gives seconds, or text such as '+1 LAP' for lapped cars, or nothing (DNF, DNS)."""
    text = row.get("gap_to_leader_text")
    if text:
        return str(text)
    seconds = _num(row.get("gap_to_leader_s"))
    if seconds is None:
        seconds = _num(row.get("gap_to_leader"))
    if seconds is None:
        return ""
    return "leader" if seconds == 0 and row.get("position") in (1, 1.0) else f"+{seconds:.3f} s"
def trend_text(slope: object, r_squared: object = None) -> str:
    """Plain description of a stint's lap-time slope. It describes the lap times, not the tyre."""
    s = _num(slope)
    if s is None:
        return ""
    fit = _num(r_squared)
    fit_txt = f", R\u00b2 {fit:.2f}" if fit is not None else ""
    if fit is not None and fit < 0.1:
        return f"no clear trend ({s:+.3f} s/lap{fit_txt}); the line explains almost none of the lap-time variation"
    if abs(s) < 0.005:
        return f"lap times roughly flat ({s:+.3f} s/lap{fit_txt})"
    if s < 0:
        return f"lap times fell {abs(s):.3f} s/lap{fit_txt}; not evidence that the tyre improved (fuel burn, track evolution)"
    return f"lap times rose {s:.3f} s/lap{fit_txt}; includes fuel-load effect, so it understates tyre wear"
def mask_weather_dropouts(df: object) -> tuple[object, dict]:
    """Treat an exact 0 that cannot be a real reading as a missing sample. OpenF1 sometimes reports 0.0 for a channel that has
    dropped out for one sample. A track temperature of exactly 0.0 beside an air temperature above 5 C (and the reverse), and a
    pressure of exactly 0, are masked to NaN. Returns a copy and the number masked per channel; the stored data is not changed."""
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df, {}
    out = df.copy()
    masked: dict[str, int] = {}
    air = pd.to_numeric(out["air_temp_c"], errors="coerce") if "air_temp_c" in out else None
    track = pd.to_numeric(out["track_temp_c"], errors="coerce") if "track_temp_c" in out else None
    rules = []
    if track is not None and air is not None:
        rules.append(("track_temp_c", (track == 0.0) & (air > 5.0)))
        rules.append(("air_temp_c", (air == 0.0) & (track > 5.0)))
    if "pressure_hpa" in out:
        rules.append(("pressure_hpa", pd.to_numeric(out["pressure_hpa"], errors="coerce") == 0.0))
    for col, mask in rules:
        n = int(mask.sum())
        if n:
            out[col] = pd.to_numeric(out[col], errors="coerce").mask(mask)
            masked[col] = n
    return out, masked
# ---------------------------------------------------------------------------
# HTML report (D11)
# ---------------------------------------------------------------------------
_REPORT_STYLE = (
    "body{font-family:sans-serif;margin:1.5rem;color:#111}"
    "table{border-collapse:collapse;margin:.5rem 0}"
    "th,td{border:1px solid #bbb;padding:.25rem .5rem;text-align:left}"
    "th{background:#eee}.na{color:#555;font-style:italic}.notice{border:1px solid #888;padding:.6rem}"
)
_WEATHER_CHANNELS = (
    ("air_temp_c", "Air temperature (C)"),
    ("track_temp_c", "Track temperature (C)"),
    ("humidity_pct", "Humidity (%)"),
    ("pressure_hpa", "Pressure (hPa)"),
    ("wind_speed_ms", "Wind speed (m/s)"),
)


def _cell_text(value: object) -> str:
    from html import escape

    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    if isinstance(value, float):
        value = round(value, 3)
    return escape(str(value), quote=True)


def _html_table(columns: list, rows: list) -> str:
    from html import escape

    head = "".join(f"<th>{escape(str(c), quote=True)}</th>" for c in columns)
    body = "".join(
        "<tr>" + "".join(f"<td>{_cell_text(r.get(c))}</td>" for c in columns) + "</tr>" for r in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _unavailable(reason: object) -> str:
    from html import escape

    return f'<p class="na">unavailable: {escape(str(reason), quote=True)}</p>'


def _section(title: str, body: str) -> str:
    return f"<section><h2>{title}</h2>{body}</section>"


def _circular_mean_deg(values: list[float]) -> float | None:
    if not values:
        return None
    s = sum(math.sin(math.radians(v)) for v in values)
    c = sum(math.cos(math.radians(v)) for v in values)
    if abs(s) < 1e-9 and abs(c) < 1e-9:
        return None
    return math.degrees(math.atan2(s, c)) % 360


def _weather_rows(weather: object) -> list[dict]:
    rows = []
    recs = _records(weather)
    for key, label in _WEATHER_CHANNELS:
        vals = [v for v in (_num(r.get(key)) for r in recs) if v is not None]
        if vals:
            rows.append({"channel": label, "min": min(vals), "max": max(vals), "mean": sum(vals) / len(vals)})
    dirs = [v for v in (_num(r.get("wind_direction_deg")) for r in recs) if v is not None]
    if dirs:
        rows.append({
            "channel": "Wind direction",
            "min": compass(min(dirs)),
            "max": compass(max(dirs)),
            "mean": compass(m) if (m := _circular_mean_deg(dirs)) is not None else None,
        })
    return rows


def report_html(
    summary: dict | None,
    results: object = None,
    standings: object = None,
    tyre_degradation: dict | None = None,
    weather: object = None,
    unavailable: dict | None = None,
    notice: str | None = None,
    weather_note: str | None = None,
) -> str:
    """Build one self-contained, escaped HTML document (D11, Requirements 7.4, 7.5, 7.7).

    Every value is passed through html.escape. A section with no data gets its
    heading and "unavailable: <reason>"; `unavailable` maps section names
    (results, standings, tyre_degradation, weather) to a reason.
    """
    from html import escape

    if notice is None:
        notice = HISTORICAL_NOTICE
    reasons = unavailable or {}

    def missing(name: str) -> str:
        return _unavailable(reasons.get(name) or "no data stored")

    summary = summary or {}
    items = [
        ("Event", summary.get("event")),
        ("Session", summary.get("session_type") or summary.get("session")),
        ("Date", summary.get("date_start") or summary.get("date")),
        ("Source", "OpenF1"),
    ]
    summary_html = "<ul>" + "".join(
        f"<li><strong>{escape(k)}:</strong> {_cell_text(v)}</li>" for k, v in items
    ) + "</ul>"

    res_rows = [{**r, "gap_to_leader": gap_display(r)} for r in _records(results)]
    res_html = (
        _html_table(["position", "driver", "laps", "points", "gap_to_leader", "dnf", "dns", "dsq"], res_rows)
        if res_rows else missing("results")
    )

    st_rows = _records(standings)
    st_html = (
        _html_table(["kind", "position", "name", "team", "points_start", "points_current"], st_rows)
        if st_rows else missing("standings")
    )

    stints = _records((tyre_degradation or {}).get("stints"))
    if stints:
        flat = []
        for s in stints:
            row = dict(s)
            ex = s.get("laps_excluded") or {}
            row["laps_excluded"] = ", ".join(f"{k}: {v}" for k, v in ex.items()) if isinstance(ex, dict) else ex
            row["observed_trend"] = trend_text(s.get("slope_s_per_lap"), s.get("r_squared"))
            flat.append(row)
        caveat = (tyre_degradation or {}).get("caveat") or FUEL_CAVEAT
        tyre_html = _html_table(
            ["driver", "stint", "compound", "tyre_age_first", "tyre_age_last", "laps_used",
             "laps_excluded", "slope_s_per_lap", "r_squared", "observed_trend", "reason"], flat
        ) + f"<p><strong>Caveat:</strong> {escape(str(caveat), quote=True)}</p>"
    else:
        tyre_html = missing("tyre_degradation")

    w_rows = _weather_rows(weather)
    weather_html = _html_table(["channel", "min", "max", "mean"], w_rows) if w_rows else missing("weather")
    if w_rows and weather_note:
        weather_html += f'<p class="na">{escape(str(weather_note), quote=True)}</p>'

    notice_html = f'<p class="notice">{escape(str(notice), quote=True)}</p><p>Data source: OpenF1</p>'

    sections = [
        _section("Summary", summary_html),
        _section("Results", res_html),
        _section("Standings", st_html),
        _section("Tyre degradation: observed lap-time trend by stint (not fuel-corrected)", tyre_html),
        _section("Weather summary", weather_html),
        _section("Historical data notice", notice_html),
    ]
    title = escape(str(summary.get("event") or "Historical session"), quote=True)
    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>{title} report</title><style>{_REPORT_STYLE}</style></head>"
        f"<body><h1>{title}</h1>{''.join(sections)}</body></html>"
    )
