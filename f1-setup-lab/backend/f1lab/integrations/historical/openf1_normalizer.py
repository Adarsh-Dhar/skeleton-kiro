"""Convert OpenF1 JSON rows into the canonical historical frames used for storage.

Field names follow https://openf1.org/docs. Nothing is invented: a value OpenF1 does not provide stays missing, and
the few values computed here (per-lap ``distance_m``, the lap a sample belongs to) are listed in the manifest as
``derived``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

PROVIDER = "openf1"
SOURCE_LABEL = "HISTORICAL F1 · OPENF1"
ATTRIBUTION = ("Data from OpenF1 (openf1.org), an unofficial open-source API. Not affiliated with Formula 1. "
               "Positions are raw OpenF1 coordinates in the provider's own units.")

SESSION_NAME_TO_TYPE = {
    "practice 1": "FP1", "practice 2": "FP2", "practice 3": "FP3", "qualifying": "Q",
    "sprint qualifying": "SQ", "sprint shootout": "SQ", "sprint": "S", "race": "R",
}
TYPE_TO_SESSION_NAMES = {
    "FP1": ("practice 1",), "FP2": ("practice 2",), "FP3": ("practice 3",), "Q": ("qualifying",),
    "SQ": ("sprint qualifying", "sprint shootout"), "S": ("sprint",), "R": ("race",),
}
MAX_GAP_S = 2.0  # samples further apart than this are not bridged when integrating distance
LAP_END_TOLERANCE_S = 2.0
MAX_RACE_CONTROL = 1000

DERIVED = [
    "distance_m: speed integrated over time within each lap (OpenF1 provides no distance channel)",
    "lap_number of car and position samples: assigned from OpenF1 lap start times",
    "session_time_s: seconds since the session start time published by OpenF1",
    "tyre_life: tyre age at stint start plus laps completed in the stint",
    "pits: true for pit-out laps and laps in which OpenF1 logged a pit stop",
]
NOT_PROVIDED = [
    "steering angle", "circuit corner positions", "stewards' lap deletions and lap validity",
    "brake pressure (OpenF1 reports only brake on/off; stored as brake_applied 0/100)",
]


# --------------------------------------------------------------------------- helpers
def parse_dt(value: Any) -> datetime | None:
    """ISO 8601 text -> aware UTC datetime, or None."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def api_time(dt: datetime, ceil: bool = False) -> str:
    """UTC datetime -> the whole-second timestamp used in OpenF1 query filters (``ceil`` rounds up for range ends)."""
    dt = dt.astimezone(UTC).replace(tzinfo=None, microsecond=0) + (timedelta(seconds=1) if ceil and dt.microsecond else timedelta(0))
    return dt.isoformat(timespec="seconds")


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if np.isnan(f) else f


def _int(v: Any) -> int | None:
    f = _num(v)
    return None if f is None else int(f)


def _rel_s(dt: datetime | None, t0: datetime | None) -> float | None:
    return None if dt is None or t0 is None else round((dt - t0).total_seconds(), 3)


def _is_testing(meeting: dict) -> bool:
    return "test" in str(meeting.get("meeting_name", "")).lower() or "test" in str(meeting.get("meeting_official_name", "")).lower()


# --------------------------------------------------------------------------- schedule
def _meetings_sorted(meetings: list[dict]) -> list[dict]:
    keep = [m for m in meetings if not _is_testing(m) and not m.get("is_cancelled")]
    return sorted(keep, key=lambda m: str(m.get("date_start") or ""))


def schedule_frame(meetings: list[dict]) -> pd.DataFrame:
    """Season schedule shaped like FastF1's (EventName, Location, ...) so event resolution can be shared."""
    rows = [{"EventName": m.get("meeting_name"), "OfficialEventName": m.get("meeting_official_name"),
             "Location": m.get("location"), "Country": m.get("country_name"), "RoundNumber": i,
             "meeting_key": m.get("meeting_key")} for i, m in enumerate(_meetings_sorted(meetings), 1)]
    return pd.DataFrame(rows, columns=["EventName", "OfficialEventName", "Location", "Country", "RoundNumber", "meeting_key"])


def schedule_sessions(sessions: list[dict]) -> list[dict]:
    out = []
    for s in sorted((s for s in sessions if not s.get("is_cancelled")), key=lambda s: str(s.get("date_start") or "")):
        name = str(s.get("session_name") or "")
        out.append({"type": SESSION_NAME_TO_TYPE.get(name.strip().lower()), "name": name, "date": str(s.get("date_start") or "")})
    return out


def event_list(meetings: list[dict], sessions: list[dict]) -> list[dict]:
    by_meeting: dict[Any, list[dict]] = {}
    for s in sessions:
        by_meeting.setdefault(s.get("meeting_key"), []).append(s)
    out = []
    for i, m in enumerate(_meetings_sorted(meetings), 1):
        out.append({"event": m.get("meeting_name"), "official_event": m.get("meeting_official_name"), "location": m.get("location"),
                    "circuit": m.get("circuit_short_name") or m.get("location"), "country": m.get("country_name"), "round": i,
                    "sessions": schedule_sessions(by_meeting.get(m.get("meeting_key"), []))})
    return out


def driver_list(rows: list[dict]) -> list[dict]:
    """[{code, number, name, team}] with one entry per driver number."""
    seen: dict[str, dict] = {}
    for r in rows:
        number = r.get("driver_number")
        if number is None or str(number) in seen:
            continue
        code = r.get("name_acronym") or str(number)
        seen[str(number)] = {"code": str(code), "number": str(number),
                             "name": r.get("full_name") or r.get("broadcast_name") or str(code), "team": r.get("team_name")}
    return list(seen.values())


def session_info(meeting: dict, session: dict, year: int, session_type: str) -> dict:
    return {"year": year, "event": meeting.get("meeting_name"), "event_official": meeting.get("meeting_official_name"),
            "location": meeting.get("location"), "round": None, "circuit": meeting.get("circuit_short_name") or meeting.get("location"),
            "session_type": session_type, "session_name": session.get("session_name") or session_type,
            "session_date": session.get("date_start"), "session_url": None}


# --------------------------------------------------------------------------- laps
def lap_windows(laps: list[dict]) -> dict[int, list[tuple[int, datetime, datetime]]]:
    """Per driver number: [(lap_number, start, end)] sorted by start. Laps without a start time are left out."""
    per: dict[int, list[tuple[int, datetime, float | None]]] = {}
    for lap in laps:
        start, n, d = parse_dt(lap.get("date_start")), _int(lap.get("lap_number")), _int(lap.get("driver_number"))
        if start is None or n is None or d is None:
            continue
        per.setdefault(d, []).append((n, start, _num(lap.get("lap_duration"))))
    out: dict[int, list[tuple[int, datetime, datetime]]] = {}
    for d, items in per.items():
        items.sort(key=lambda t: t[1])
        wins = []
        for i, (n, start, dur) in enumerate(items):
            nxt = items[i + 1][1] if i + 1 < len(items) else None
            cap = start + timedelta(seconds=dur + LAP_END_TOLERANCE_S) if dur else None
            end = min(x for x in (nxt, cap) if x is not None) if (nxt or cap) else start
            wins.append((n, start, end))
        out[d] = wins
    return out


def normalize_laps(laps: list[dict], stints: list[dict], pit: list[dict], drivers: list[dict]) -> pd.DataFrame:
    """One row per driver lap. Only fields OpenF1 provides are filled; validity flags stay absent."""
    by_number = {str(d["number"]): d for d in drivers}
    stint_by_driver: dict[int, list[dict]] = {}
    for s in stints:
        if _int(s.get("driver_number")) is not None:
            stint_by_driver.setdefault(_int(s["driver_number"]), []).append(s)
    pit_laps = {(_int(p.get("driver_number")), _int(p.get("lap_number"))) for p in pit}
    rows = []
    for lap in sorted(laps, key=lambda r: (_int(r.get("driver_number")) or 0, _int(r.get("lap_number")) or 0)):
        number, n = _int(lap.get("driver_number")), _int(lap.get("lap_number"))
        drv = by_number.get(str(number))
        if drv is None or n is None:
            continue
        stint = next((s for s in stint_by_driver.get(number, [])
                      if (_int(s.get("lap_start")) or 0) <= n <= (_int(s.get("lap_end")) or 10**6)), None)
        age = _int(stint.get("tyre_age_at_start")) if stint else None
        start = _int(stint.get("lap_start")) if stint else None
        rows.append({
            "driver": drv["code"], "driver_number": number, "team": drv.get("team"), "lap_number": n,
            "stint": _int(stint.get("stint_number")) if stint else None,
            "lap_time_s": _num(lap.get("lap_duration")),
            "sector_1_s": _num(lap.get("duration_sector_1")), "sector_2_s": _num(lap.get("duration_sector_2")),
            "sector_3_s": _num(lap.get("duration_sector_3")),
            "compound": stint.get("compound") if stint else None,
            "tyre_life": age + (n - start) if age is not None and start is not None else None,
            "pits": bool(lap.get("is_pit_out_lap")) or (number, n) in pit_laps,
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    for c in ("driver_number", "lap_number", "stint", "tyre_life"):
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("Int64")
    for c in ("lap_time_s", "sector_1_s", "sector_2_s", "sector_3_s"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["pits"] = df["pits"].astype("boolean")
    return df


# --------------------------------------------------------------------------- samples
def _assign_laps(times: np.ndarray, windows: list[tuple[int, datetime, datetime]]) -> np.ndarray:
    """Lap number for each timestamp (epoch seconds), or 0 where no lap window contains it."""
    if not windows:
        return np.zeros(len(times), dtype=int)
    starts = np.array([w[1].timestamp() for w in windows])
    ends = np.array([w[2].timestamp() for w in windows])
    nums = np.array([w[0] for w in windows])
    idx = np.searchsorted(starts, times, side="right") - 1
    ok = (idx >= 0) & (times < ends[np.clip(idx, 0, None)])
    return np.where(ok, nums[np.clip(idx, 0, None)], 0)


def _sample_times(rows: list[dict]) -> tuple[list[dict], np.ndarray]:
    parsed = [(parse_dt(r.get("date")), r) for r in rows]
    parsed = sorted(((d, r) for d, r in parsed if d is not None), key=lambda t: t[0])
    kept, seen = [], set()
    for d, r in parsed:  # chunk edges can repeat a sample
        if d in seen:
            continue
        seen.add(d)
        kept.append(r)
    return kept, np.array([parse_dt(r["date"]).timestamp() for r in kept])


def car_frame(rows: list[dict], windows: list[tuple[int, datetime, datetime]], t0: datetime | None) -> pd.DataFrame:
    """Canonical telemetry for one driver. ``distance_m`` is derived per lap; brake becomes ``brake_applied`` (0/100)."""
    rows, times = _sample_times(rows)
    if not rows:
        return pd.DataFrame()
    laps = _assign_laps(times, windows)
    speed = np.array([_num(r.get("speed")) if _num(r.get("speed")) is not None else np.nan for r in rows])
    df = pd.DataFrame({
        "lap_number": laps,
        "session_time_s": [None if t0 is None else round(t - t0.timestamp(), 3) for t in times],
        "speed_kph": speed,
        "throttle_pct": [_num(r.get("throttle")) for r in rows],
        "brake_applied": [None if _num(r.get("brake")) is None else (100.0 if _num(r.get("brake")) > 0 else 0.0) for r in rows],
        "gear": [_num(r.get("n_gear")) for r in rows],
        "rpm": [_num(r.get("rpm")) for r in rows],
        "drs": [_num(r.get("drs")) for r in rows],
    })
    dist = np.full(len(df), np.nan)
    for lap in np.unique(laps[laps > 0]):
        idx = np.flatnonzero(laps == lap)
        t, v = times[idx], np.nan_to_num(speed[idx], nan=0.0) / 3.6
        step = np.diff(t)
        inc = np.where(step <= MAX_GAP_S, (v[1:] + v[:-1]) / 2 * step, 0.0)
        dist[idx] = np.concatenate([[0.0], np.cumsum(inc)])
    df.insert(1, "distance_m", np.round(dist, 2))
    df = df[df["lap_number"] > 0]
    for c in df.columns:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["lap_number"] = df["lap_number"].astype(int)
    return df.reset_index(drop=True)


def location_frame(rows: list[dict], windows: list[tuple[int, datetime, datetime]], t0: datetime | None) -> pd.DataFrame:
    """Track position for one driver. Coordinates stay in OpenF1's own units; (0,0,0) 'no signal' samples are dropped."""
    rows, times = _sample_times(rows)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame({
        "lap_number": _assign_laps(times, windows),
        "session_time_s": [None if t0 is None else round(t - t0.timestamp(), 3) for t in times],
        "x_m": [_num(r.get("x")) for r in rows], "y_m": [_num(r.get("y")) for r in rows], "z_m": [_num(r.get("z")) for r in rows],
    })
    for c in df.columns:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df[(df["lap_number"] > 0) & df[["x_m", "y_m"]].notna().all(axis=1) & ~((df["x_m"] == 0) & (df["y_m"] == 0) & (df["z_m"].fillna(0) == 0))]
    df["lap_number"] = df["lap_number"].astype(int)
    return df.reset_index(drop=True)


def normalize_weather(rows: list[dict], t0: datetime | None) -> pd.DataFrame:
    """Weather samples. ``rainfall`` is an on/off flag in OpenF1, so it is stored as ``rainfall_flag`` (not mm)."""
    out = []
    for r in sorted(rows, key=lambda r: str(r.get("date") or "")):
        out.append({"time_s": _rel_s(parse_dt(r.get("date")), t0), "air_temp_c": _num(r.get("air_temperature")),
                    "track_temp_c": _num(r.get("track_temperature")), "humidity_pct": _num(r.get("humidity")),
                    "wind_speed_ms": _num(r.get("wind_speed")), "pressure_hpa": _num(r.get("pressure")),
                    "wind_direction_deg": _num(r.get("wind_direction")), "rainfall_flag": _num(r.get("rainfall"))})
    return pd.DataFrame(out)


def race_control(rows: list[dict], t0: datetime | None) -> list[dict]:
    out = []
    for r in sorted(rows, key=lambda r: str(r.get("date") or ""))[:MAX_RACE_CONTROL]:
        out.append({"time_s": _rel_s(parse_dt(r.get("date")), t0), "date": r.get("date"), "category": r.get("category"),
                    "flag": r.get("flag"), "scope": r.get("scope"), "lap_number": _int(r.get("lap_number")),
                    "driver_number": _int(r.get("driver_number")), "message": str(r.get("message") or "")[:300]})
    return out


def pit_stops(rows: list[dict], drivers: list[dict]) -> list[dict]:
    code = {str(d["number"]): d["code"] for d in drivers}
    out = []
    for r in sorted(rows, key=lambda r: (_int(r.get("lap_number")) or 0, str(r.get("date") or ""))):
        number = str(r.get("driver_number"))
        if number not in code:
            continue
        lane = _num(r.get("lane_duration"))
        out.append({"driver": code[number], "lap_number": _int(r.get("lap_number")), "date": r.get("date"),
                    "lane_duration_s": lane if lane is not None else _num(r.get("pit_duration")),
                    "stop_duration_s": _num(r.get("stop_duration"))})
    return out


def build_manifest(info: dict, drivers: list[dict], channels: dict, session_slug: str) -> dict:
    return {
        "provider": PROVIDER, "source_label": SOURCE_LABEL, "session_id": session_slug, "year": info["year"],
        "event": info["event"], "event_official": info.get("event_official"), "location": info.get("location"),
        "round": info.get("round"), "circuit": info.get("circuit"), "session": info["session_name"],
        "session_type": info["session_type"], "session_date": info.get("session_date"), "session_url": info.get("session_url"),
        "imported_at": datetime.now(UTC).isoformat(), "drivers": drivers, "channels": channels,
        "derived": DERIVED, "not_provided": NOT_PROVIDED, "attribution": ATTRIBUTION, "import_status": "COMPLETED",
    }
