"""Convert FastF1 session objects into canonical records/dataframes for storage.

Everything here is defensive about column names: FastF1 versions rename telemetry columns, and missing
channels stay missing (null) rather than being fabricated as zeros.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pandas as pd

PROVIDER = "fastf1"
SOURCE_LABEL = "HISTORICAL F1 · FASTF1"


def _td_s(v: Any) -> float | None:
    """Timedelta/Timestamp/number -> seconds, or None."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        if hasattr(v, "total_seconds"):
            return round(float(v.total_seconds()), 4)
        return round(float(v), 4)
    except (TypeError, ValueError):
        return None


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
        return None if pd.isna(f) else round(f, 4)
    except (TypeError, ValueError):
        return None


def _pick(row: Any, *names: str) -> Any:
    for n in names:
        if n in row.index:
            return row[n]
    return None


def _col(df: pd.DataFrame, *names: str) -> str | None:
    for n in names:
        if n in df.columns:
            return n
    return None


def _drv_get(drv: Any, *names: str) -> Any:
    """Read a field from a FastF1 DriverResult (Series), dict, or plain object."""
    if drv is None:
        return None
    for n in names:
        try:
            if hasattr(drv, "get") and drv.get(n) is not None:
                return drv.get(n)
        except Exception:
            pass
        v = getattr(drv, n, None)
        if v is not None:
            return v
    return None


def driver_list(session) -> list[dict]:
    """[{code, number, name, team}] for every driver in the session.

    FastF1 3.9 ``session.drivers`` yields driver numbers; the three-letter code lives in the
    DriverResult's ``Abbreviation`` field. Both are kept so lap tables (which may key on either)
    can be matched.
    """
    out = []
    for item in list(getattr(session, "drivers", []) or []):
        try:
            drv = session.get_driver(item)
        except Exception:
            drv = None
        code = _drv_get(drv, "Abbreviation", "abbreviation") or _drv_get(drv, "DriverId", "driver_id") or str(item)
        first = _drv_get(drv, "FirstName", "first_name")
        last = _drv_get(drv, "LastName", "last_name")
        full = _drv_get(drv, "FullName", "full_name")
        name = full or " ".join(x for x in (first or "", last or "") if x) or str(code)
        number = _drv_get(drv, "DriverNumber", "number", "driver_number")
        out.append({"code": str(code), "number": str(number) if _present(number) else str(item),
                    "name": name or str(code), "team": _drv_get(drv, "TeamName", "team_name", "Team")})
    return out


def _driver_mask(laps: pd.DataFrame, code: str, number: str | None = None):
    """Boolean mask matching the laps 'Driver' column against code and/or number (versions differ)."""
    if "Driver" not in laps.columns:
        return laps.index == code
    vals = laps["Driver"].astype(str)
    keys = {str(code)} | ({str(number)} if number else set())
    return vals.isin(keys)


def _present(v: Any) -> bool:
    """Truthiness that is safe for pandas NA/NaT/Series-like values."""
    if v is None:
        return False
    try:
        if pd.isna(v):
            return False
    except (TypeError, ValueError):
        pass
    return True


def _flag(v: Any) -> bool | None:
    """None-safe boolean for timing flags (Deleted, IsValid, IsAccurate, ...)."""
    return bool(v) if _present(v) else None


def _pit_flag(lap: Any) -> bool:
    """A lap is a pit lap when FastF1 recorded PitInTime/PitOutTime (or a legacy Pits flag)."""
    for n in ("PitInTime", "PitOutTime", "pit_in_time", "pit_out_time"):
        if _present(_pick(lap, n)):
            return True
    p = _pick(lap, "Pits", "pits")
    return bool(_flag(p)) if _present(p) else False


def session_info(session, year: int, event: str, session_type: str) -> dict:
    """Canonical session metadata for the manifest."""
    ev = getattr(session, "event", None)
    if ev is None:
        ev = {}
    get = ev.get if hasattr(ev, "get") else lambda k, d=None: getattr(ev, k, d)
    date = get("SessionDate")
    if not _present(date):
        date = get("EventDate")
    if hasattr(date, "isoformat") and _present(date):
        date = date.isoformat()
    # FastF1's session_info JSON carries Meeting.Circuit.{ShortName,OfficialName}
    meeting_circuit = {}
    try:
        si = getattr(session, "session_info", None) or {}
        meeting_circuit = (si.get("Meeting", {}) or {}).get("Circuit", {}) or {}
    except Exception:
        meeting_circuit = {}
    return {
        "year": year,
        "event": get("EventName") or get("OfficialEventName") or event,
        "event_official": get("OfficialEventName"),
        "location": get("Location"),
        "round": get("Round") or get("RoundNumber"),
        "circuit": (meeting_circuit.get("ShortName") or meeting_circuit.get("OfficialName")
                    or get("CircuitShortName") or get("CircuitName")),
        "session_type": session_type,
        "session_name": get("SessionName") or session_type,
        "session_date": str(date) if date else None,
        "session_url": get("SessionUrl"),
    }


def _session_laps(session):
    """session.laps, or None when the timing feed could not be loaded (raises DataNotLoadedError)."""
    try:
        laps = getattr(session, "laps", None)
        return laps if laps is not None and len(laps) else None
    except Exception:
        return None


def normalize_laps(session) -> pd.DataFrame:
    """Canonical lap table: one row per driver lap."""
    laps = _session_laps(session)
    if laps is None:
        return pd.DataFrame()
    rows = []
    for _, lap in laps.iterrows():
        rows.append({
            "driver": _pick(lap, "Driver", "driver"),
            "driver_number": _num(_pick(lap, "DriverNumber", "Number")),
            "team": _pick(lap, "Team", "TeamName"),
            "lap_number": _num(_pick(lap, "LapNumber", "lap_number")),
            "stint": _num(_pick(lap, "Stint", "stint")),
            "lap_time_s": _td_s(_pick(lap, "LapTime", "lap_time")),
            "sector_1_s": _td_s(_pick(lap, "Sector1Time", "sector_1")),
            "sector_2_s": _td_s(_pick(lap, "Sector2Time", "sector_2")),
            "sector_3_s": _td_s(_pick(lap, "Sector3Time", "sector_3")),
            "sector_4_s": _td_s(_pick(lap, "Sector4Time", "sector_4")),
            "compound": _pick(lap, "Compound", "compound"),
            "tyre_life": _num(_pick(lap, "TyreLife", "tyre_life")),
            "is_personal_best": _flag(_pick(lap, "IsPersonalBest", "is_personal_best")),
            # FastF1 semantics: 'Deleted' = lap removed by the stewards; 'IsAccurate' = timing
            # alignment quality. They are independent concepts and must not be conflated.
            "is_deleted": _flag(_pick(lap, "Deleted", "is_deleted")),
            "is_accurate": _flag(_pick(lap, "IsAccurate", "is_accurate")),
            "deleted_reason": _pick(lap, "DeletedReason", "deleted_reason"),
            "is_valid": _flag(_pick(lap, "IsValid", "is_valid")),
            "pits": _pit_flag(lap),
        })
    df = pd.DataFrame(rows)
    for c in ("is_deleted", "is_accurate", "is_valid", "is_personal_best"):
        if c in df.columns:
            df[c] = df[c].map(_flag).astype("boolean")
    if "is_valid" in df.columns:
        # Validity: when steward deletion info exists it is authoritative (Deleted=True => invalid).
        # Otherwise keep an explicit IsValid flag. IsAccurate is never used for validity.
        deleted = df["is_deleted"] if "is_deleted" in df.columns else pd.Series([pd.NA] * len(df), dtype="boolean")
        raw_valid = df["is_valid"]
        df["is_valid"] = pd.array(
            [(not bool(d)) if pd.notna(d) else (bool(v) if pd.notna(v) else pd.NA)
             for d, v in zip(deleted.tolist(), raw_valid.tolist())],
            dtype="boolean",
        )
    for c in ("driver_number", "lap_number", "stint", "lap_time_s", "sector_1_s", "sector_2_s",
              "sector_3_s", "sector_4_s", "tyre_life"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    for c in ("driver_number", "lap_number", "stint", "tyre_life"):
        if c in df.columns:
            df[c] = df[c].astype("Int64")
    return df


def normalize_weather(session) -> pd.DataFrame:
    """Weather timeline. Weather is sampled sparsely by the timing feed - do not upsample."""
    try:
        wx = getattr(session, "weather_data", None)
    except Exception:
        wx = None
    if wx is None or not len(wx):
        return pd.DataFrame()
    rows = []
    t0 = None
    for _, w in wx.iterrows():
        t = _pick(w, "Time", "SessionTime", "time")
        if t0 is None and hasattr(t, "total_seconds"):
            t0 = t
        rows.append({
            "time_s": _td_s(t),
            "air_temp_c": _num(_pick(w, "AirTemp", "air_temp")),
            "track_temp_c": _num(_pick(w, "TrackTemp", "track_temp")),
            "humidity_pct": _num(_pick(w, "Humidity", "humidity")),
            "wind_speed_ms": _num(_pick(w, "WindSpeed", "wind_speed")),
            "rainfall_mm": _num(_pick(w, "Rainfall", "rainfall")),
            "pressure_hpa": _num(_pick(w, "Pressure", "pressure")),
        })
    df = pd.DataFrame(rows)
    if "time_s" in df.columns:
        df["time_s"] = df["time_s"].astype("float64")
    return df


def _brake_channels(vals) -> tuple[list | None, list | None]:
    """Split the raw Brake channel into (brake_applied, brake_pct).

    FastF1 publishes ``Brake`` as a boolean 'brakes applied' flag (unlike ``Throttle``, which is a
    0-100 percentage). An on/off signal must never be stored as a measured brake percentage, so
    boolean/0-1 data becomes ``brake_applied`` on a 0/100 scale; only genuine percentages (max > 1)
    are kept as ``brake_pct``.
    """
    if vals is None:
        return None, None
    nums = pd.to_numeric(pd.Series(list(vals)), errors="coerce")
    finite = nums.dropna()
    if finite.empty:
        return None, None
    is_flag = str(getattr(vals, "dtype", "")) in ("bool", "boolean") or float(finite.max()) <= 1.0
    if is_flag:
        return [None if pd.isna(v) else (100.0 if float(v) > 0 else 0.0) for v in nums], None
    return None, [_num(v) for v in vals]


def _car_frame(car: pd.DataFrame) -> pd.DataFrame:
    """Canonical per-sample car telemetry frame (no lap column yet)."""
    if car is None or not len(car):
        return pd.DataFrame()
    c = car.copy()
    # per-lap distance: prefer an existing Distance column, else integrate via FastF1's add_distance()
    if _col(c, "Distance", "distance") is None:
        try:
            c = c.add_distance()
        except Exception:
            pass
    dist_col = _col(c, "Distance", "distance")
    brake_col = _col(c, "Brake", "brake")
    brake_applied, brake_pct = _brake_channels(c[brake_col] if brake_col else None)
    out = pd.DataFrame({
        "distance_m": c[dist_col] if dist_col else pd.NA,
        "session_time_s": [_td_s(v) for v in c[_col(c, "SessionTime", "session_time")] ] if _col(c, "SessionTime", "session_time") else None,
        "speed_kph": [_num(v) for v in c[_col(c, "Speed", "speed")]] if _col(c, "Speed", "speed") else None,
        "throttle_pct": [_num(v) for v in c[_col(c, "Throttle", "throttle")]] if _col(c, "Throttle", "throttle") else None,
        "brake_applied": brake_applied,
        "brake_pct": brake_pct,
        "steer_pct": [_num(v) for v in c[_col(c, "Steer", "Steering", "steer", "steering")]] if _col(c, "Steer", "Steering", "steer", "steering") else None,
        "gear": [_num(v) for v in c[_col(c, "Gear", "nGear", "gear")]] if _col(c, "Gear", "nGear", "gear") else None,
        "rpm": [_num(v) for v in c[_col(c, "RPM", "rpm")]] if _col(c, "RPM", "rpm") else None,
        "drs": [_num(v) for v in c[_col(c, "DRS", "drs")]] if _col(c, "DRS", "drs") else None,
    })
    # exactly one brake representation exists; drop the unused one
    for col in ("brake_applied", "brake_pct"):
        if out[col].isna().all():
            out = out.drop(columns=[col])
    # canonical telemetry columns are all numeric; coerce so parquet storage gets real dtypes
    for col in out.columns:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    # keep only rows with at least one real measurement
    keep = out.notna().any(axis=1)
    return out[keep].reset_index(drop=True)


def driver_telemetry(session, driver_code: str, driver_number: str | None = None, *, telemetry: bool = True) -> pd.DataFrame:
    """All laps of one driver concatenated, with a lap_number column. Columns are canonical snake_case."""
    if not telemetry:
        return pd.DataFrame()
    laps = _session_laps(session)
    if laps is None:
        return pd.DataFrame()
    mask = _driver_mask(laps, driver_code, driver_number)
    frames = []
    for _, lap in laps[mask].iterrows():
        if _pick(lap, "LapTime", "lap_time") is None:
            continue
        try:
            car = lap.get_car_data()
        except Exception:
            car = None
        df = _car_frame(car)
        if df.empty:
            continue
        df.insert(0, "lap_number", int(_pick(lap, "LapNumber", "lap_number") or 0))
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def lap_telemetry(session, driver_code: str, lap_number: int, driver_number: str | None = None) -> pd.DataFrame:
    """Car telemetry for a single lap."""
    df = driver_telemetry(session, driver_code, driver_number)
    if df.empty:
        return df
    return df[df["lap_number"] == lap_number].reset_index(drop=True)


def _pos_frame(pos: pd.DataFrame) -> pd.DataFrame:
    if pos is None or not len(pos):
        return pd.DataFrame()
    p = pos.copy()
    dist_col = _col(p, "Distance", "distance")
    if dist_col is None:
        try:
            p = p.add_distance()
            dist_col = _col(p, "Distance", "distance")
        except Exception:
            dist_col = None
    x_col, y_col = _col(p, "X", "x"), _col(p, "Y", "y")
    if x_col is None or y_col is None:
        return pd.DataFrame()
    out = pd.DataFrame({
        "distance_m": p[dist_col] if dist_col else pd.NA,
        "session_time_s": [_td_s(v) for v in p[_col(p, "SessionTime", "session_time")]] if _col(p, "SessionTime", "session_time") else None,
        "x_m": [_num(v) for v in p[x_col]],
        "y_m": [_num(v) for v in p[y_col]],
        "z_m": [_num(v) for v in p[_col(p, "Z", "z")]] if _col(p, "Z", "z") else None,
        "speed_kph": [_num(v) for v in p[_col(p, "Speed", "speed")]] if _col(p, "Speed", "speed") else None,
    })
    for col in out.columns:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    keep = out[["x_m", "y_m"]].notna().all(axis=1)
    return out[keep].reset_index(drop=True)


def driver_position(session, driver_code: str, driver_number: str | None = None, *, position: bool = True) -> pd.DataFrame:
    """Track position (X/Y/Z) for all laps of one driver, with a lap_number column."""
    if not position:
        return pd.DataFrame()
    laps = _session_laps(session)
    if laps is None:
        return pd.DataFrame()
    mask = _driver_mask(laps, driver_code, driver_number)
    frames = []
    for _, lap in laps[mask].iterrows():
        try:
            pos = lap.get_pos_data()
        except Exception:
            pos = None
        df = _pos_frame(pos)
        if df.empty:
            continue
        df.insert(0, "lap_number", int(_pick(lap, "LapNumber", "lap_number") or 0))
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _circuit_info(session) -> Any:
    """FastF1 >=3.x exposes ``session.get_circuit_info()`` returning a CircuitInfo dataclass with a
    ``corners`` DataFrame (X, Y, Number, Letter, Angle, Distance). Test doubles and older shims may
    instead carry a ``circuit_info`` attribute or a dict with a Turns/corners table."""
    getter = getattr(session, "get_circuit_info", None)
    if callable(getter):
        try:
            info = getter()
            if info is not None:
                return info
        except Exception:
            pass
    return getattr(session, "circuit_info", None)


def circuit_corners(session) -> list[dict]:
    """Corner markers [{number,name,letter,distance_m,x_m,y_m}] from FastF1 circuit info."""
    info = _circuit_info(session)
    if info is None:
        return []
    corners = getattr(info, "corners", None)
    if corners is None and hasattr(info, "get"):
        corners = info.get("corners") or info.get("Corners") or info.get("Turns")
    if corners is None or not len(corners):
        return []
    out = []
    for _, t in corners.iterrows():
        out.append({
            "number": _num(t.get("Number", t.get("number"))),
            "name": t.get("Name", t.get("name")),
            "letter": t.get("Letter", t.get("letter")),
            "distance_m": _num(t.get("Distance", t.get("distance"))),
            "x_m": _num(t.get("X", t.get("x"))),
            "y_m": _num(t.get("Y", t.get("y"))),
        })
    return out


def build_manifest(info: dict, drivers: list[dict], channels: dict, session_slug: str) -> dict:
    return {
        "provider": PROVIDER,
        "source_label": SOURCE_LABEL,
        "session_id": session_slug,
        "year": info["year"],
        "event": info["event"],
        "event_official": info.get("event_official"),
        "location": info.get("location"),
        "round": info.get("round"),
        "circuit": info.get("circuit"),
        "session": info["session_name"],
        "session_type": info["session_type"],
        "session_date": info.get("session_date"),
        "session_url": info.get("session_url"),
        "imported_at": datetime.now(UTC).isoformat(),
        "drivers": drivers,
        "channels": channels,
        "attribution": "Data via the FastF1 library (unofficial F1 timing data). Not affiliated with Formula 1.",
        "import_status": "COMPLETED",
    }
