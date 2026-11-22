"""Import one historical session from OpenF1 into the same normalized storage the FastF1 importer uses."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

import pandas as pd

from f1lab.integrations.historical import openf1_normalizer as N
from f1lab.integrations.historical.importer import (
    DRIVER_NOT_FOUND,
    EVENT_NOT_FOUND,
    INVALID_REQUEST,
    NO_LAP_DATA,
    SESSION_UNAVAILABLE,
    HistoricalImportError,
    ImportCancelled,
    _session_code,
    resolve_event,
)
from f1lab.integrations.historical.openf1_client import BASE_URL, MIN_YEAR, OpenF1Client, OpenF1Error
from f1lab.integrations.historical.repository import HistoricalRepository, session_id

Progress = Callable[[str, str], None]
CHUNK = timedelta(minutes=30)  # car_data / location are requested in windows this long (3.7 Hz keeps each answer small)


def check_year(year: int) -> None:
    if year < MIN_YEAR:
        raise HistoricalImportError(INVALID_REQUEST, f"OpenF1 historical data starts in {MIN_YEAR}; use the FastF1 provider for {year}")


def meetings_for(client: OpenF1Client, year: int, cancel=None) -> list[dict]:
    check_year(year)
    rows = client.get("meetings", {"year": year}, cancel=cancel)
    if not N.schedule_frame(rows).shape[0]:
        raise HistoricalImportError(SESSION_UNAVAILABLE, f"OpenF1 has no {year} meetings")
    return rows


def resolve_meeting(meetings: list[dict], event: str) -> dict:
    """Map a user-supplied event string to an OpenF1 meeting (same matching rules as the FastF1 schedule)."""
    name = resolve_event(N.schedule_frame(meetings), event)
    meeting = next((m for m in meetings if m.get("meeting_name") == name), None)
    if meeting is None:
        raise HistoricalImportError(EVENT_NOT_FOUND, f"no Grand Prix matching '{event}' in the season schedule")
    return meeting


def pick_session(sessions: list[dict], code: str) -> dict:
    names = N.TYPE_TO_SESSION_NAMES[code]
    for s in sessions:
        if str(s.get("session_name", "")).strip().lower() in names and not s.get("is_cancelled"):
            return s
    raise HistoricalImportError(SESSION_UNAVAILABLE, f"this event has no {code} session in OpenF1")


def select_drivers(all_drivers: list[dict], wanted: list[str]) -> list[dict]:
    want = [d.strip().upper() for d in wanted]
    chosen = [d for d in all_drivers if not want or d["code"].upper() in want or str(d["number"]).upper() in want]
    if want:
        have = {d["code"].upper() for d in chosen} | {str(d["number"]).upper() for d in chosen}
        missing = [d for d in want if d not in have]
        if missing:
            raise HistoricalImportError(DRIVER_NOT_FOUND, f"driver(s) not in this session: {', '.join(missing)}")
    if not chosen:
        raise HistoricalImportError(DRIVER_NOT_FOUND, "no drivers selected")
    return chosen


def preview(client: OpenF1Client, year: int, event: str, session: str) -> list[dict]:
    """Driver list of a session without importing anything."""
    code = _session_code(session)
    meeting = resolve_meeting(meetings_for(client, year), event)
    sess = pick_session(client.get("sessions", {"meeting_key": meeting["meeting_key"]}), code)
    return N.driver_list(client.get("drivers", {"session_key": sess["session_key"]}))


def covers(manifest: dict, payload: dict) -> bool:
    """A stored OpenF1 session satisfies a request when every wanted driver was already attempted for every wanted channel
    (an attempt that returned no data counts: asking again would not produce any)."""
    stored = {str(d.get("code", "")).upper() for d in manifest.get("drivers", [])} | {str(d.get("number", "")).upper() for d in manifest.get("drivers", [])}
    wanted = {d.strip().upper() for d in payload.get("drivers") or []}
    if not wanted and not manifest.get("all_drivers"):
        return False
    if not wanted <= stored:
        return False
    codes = [d["code"] for d in manifest.get("drivers", []) if not wanted or d["code"].upper() in wanted or str(d.get("number", "")).upper() in wanted]
    ch = manifest.get("channels", {})
    if payload.get("include_telemetry", True) and any(c not in ch.get("telemetry", {}) for c in codes):
        return False
    if payload.get("include_position", True) and any(c not in ch.get("position", {}) for c in codes):
        return False
    return not (payload.get("include_weather", True) and "weather" not in ch)


def _chunks(start, end):
    cur = start
    while cur < end:
        nxt = min(cur + CHUNK, end)
        yield cur, nxt
        cur = nxt


def import_session_openf1(client: OpenF1Client, repo: HistoricalRepository, payload: dict, progress: Progress | None = None, *,
                          should_cancel: Callable[[], bool] | None = None, force: bool = True) -> dict:
    """Run one OpenF1 import synchronously (call via asyncio.to_thread). Returns the stored manifest.

    The lap table is required. Weather, stints, pit stops, race control and each driver's car/position data are optional:
    when one of them is empty or fails the session is still stored, marked ``partial``, and ``coverage`` records what each
    endpoint returned. Cancelling (or a failure of a required step) stores nothing.
    """
    say = progress or (lambda *_: None)

    def cancelled() -> bool:
        return bool(should_cancel and should_cancel())

    def checkpoint() -> None:
        if cancelled():
            raise ImportCancelled("import cancelled")

    year = int(payload["year"])
    check_year(year)
    code = _session_code(str(payload["session"]))

    say("DOWNLOADING", f"loading the {year} OpenF1 meetings")
    meeting = resolve_meeting(meetings_for(client, year, cancelled), str(payload["event"]))
    sid = session_id(year, meeting["meeting_name"], code, "openf1")
    checkpoint()

    if not force:
        existing = repo.manifest(sid)
        if existing is not None and covers(existing, payload):
            say("COMPLETED", f"{meeting['meeting_name']} {code} is already stored; reusing it (no download)")
            return {**existing, "reused": True}

    sess = pick_session(client.get("sessions", {"meeting_key": meeting["meeting_key"]}, cancel=cancelled), code)
    sk = sess["session_key"]
    say("DOWNLOADING", f"downloading {meeting['meeting_name']} {code} from OpenF1 (cache: {client.cache_dir})")
    drivers_all = N.driver_list(client.get("drivers", {"session_key": sk}, cancel=cancelled))
    if not drivers_all:
        raise HistoricalImportError(SESSION_UNAVAILABLE, f"{meeting['meeting_name']} {code} has no driver list in OpenF1 (session not held or data not published)")
    wanted = [d.strip().upper() for d in payload.get("drivers") or []]
    drivers = select_drivers(drivers_all, wanted)
    checkpoint()

    coverage: dict = {"endpoints": {}, "drivers_empty": {"car_data": [], "location": []}}
    warnings: list[str] = []
    partial = False

    def optional(name: str, endpoint: str, filters: dict) -> list[dict]:
        nonlocal partial
        try:
            rows = client.get(endpoint, filters, cancel=cancelled)
        except OpenF1Error as exc:
            coverage["endpoints"][name] = {"rows": 0, "status": "error", "note": str(exc)}
            warnings.append(f"{name} unavailable: {exc}")
            partial = True
            return []
        coverage["endpoints"][name] = {"rows": len(rows), "status": "ok" if rows else "empty"}
        return rows

    laps_raw = client.get("laps", {"session_key": sk}, cancel=cancelled)
    coverage["endpoints"]["laps"] = {"rows": len(laps_raw), "status": "ok" if laps_raw else "empty"}
    if not laps_raw:
        raise HistoricalImportError(NO_LAP_DATA, "OpenF1 has no lap timing data for this session (it may not be published yet)")
    checkpoint()
    stints_raw = optional("stints", "stints", {"session_key": sk})
    pit_raw = optional("pit", "pit", {"session_key": sk})
    rc_raw = optional("race_control", "race_control", {"session_key": sk})
    weather_raw = optional("weather", "weather", {"session_key": sk}) if payload.get("include_weather", True) else []
    checkpoint()

    say("PROCESSING", "normalizing laps and session metadata")
    laps = N.normalize_laps([r for r in laps_raw if str(r.get("driver_number")) in {d["number"] for d in drivers}], stints_raw, pit_raw, drivers)
    if laps.empty:
        raise HistoricalImportError(NO_LAP_DATA, "OpenF1 returned no laps for the selected drivers")
    t0 = N.parse_dt(sess.get("date_start"))
    windows = N.lap_windows(laps_raw)
    weather = N.normalize_weather(weather_raw, t0) if payload.get("include_weather", True) else pd.DataFrame()
    channels: dict = {"laps": True, "telemetry": {}, "position": {}}
    if payload.get("include_weather", True):
        channels["weather"] = not weather.empty
        if weather.empty:
            warnings.append("no weather samples in this session")

    def per_driver(endpoint: str, d: dict, builder) -> pd.DataFrame:
        nonlocal partial
        number = int(d["number"])
        wins = windows.get(number, [])
        if not wins:
            coverage["endpoints"].setdefault(endpoint, {"rows": 0, "status": "empty"})
            coverage["drivers_empty"][endpoint].append(d["code"])
            warnings.append(f"no {endpoint} for {d['code']} (no lap start times to anchor it)")
            partial = True
            return pd.DataFrame()
        rows: list[dict] = []
        try:
            for a, b in _chunks(min(w[1] for w in wins), max(w[2] for w in wins)):
                rows += client.get(endpoint, {"session_key": sk, "driver_number": number},
                                   ranges=(("date", ">=", N.api_time(a)), ("date", "<", N.api_time(b, ceil=True))), cancel=cancelled)
        except OpenF1Error as exc:
            warnings.append(f"{endpoint} for {d['code']} failed: {exc}")
            coverage["drivers_empty"][endpoint].append(d["code"])
            partial = True
            return pd.DataFrame()
        df = builder(rows, wins, t0)
        if df.empty:
            coverage["drivers_empty"][endpoint].append(d["code"])
            warnings.append(f"no {'car telemetry' if endpoint == 'car_data' else 'track position'} for {d['code']}")
            partial = True
        agg = coverage["endpoints"].setdefault(endpoint, {"rows": 0, "status": "ok"})
        agg["rows"] += len(df)
        return df

    telemetry: dict[str, pd.DataFrame] = {}
    position: dict[str, pd.DataFrame] = {}
    for d in drivers:
        checkpoint()
        if payload.get("include_telemetry", True):
            say("PROCESSING", f"downloading car data for {d['code']}")
            telemetry[d["code"]] = per_driver("car_data", d, N.car_frame)
            channels["telemetry"][d["code"]] = not telemetry[d["code"]].empty
        if payload.get("include_position", True):
            say("PROCESSING", f"downloading track position for {d['code']}")
            position[d["code"]] = per_driver("location", d, N.location_frame)
            channels["position"][d["code"]] = not position[d["code"]].empty
    for name in ("car_data", "location"):
        if name in coverage["endpoints"] and coverage["endpoints"][name]["rows"] == 0:
            coverage["endpoints"][name]["status"] = "empty"

    manifest = N.build_manifest(N.session_info(meeting, sess, year, code), drivers, channels, sid)
    manifest.update(corners=[], all_drivers=not wanted, warnings=warnings, coverage=coverage, partial=partial,
                    race_control=N.race_control(rc_raw, t0), pit_stops=N.pit_stops(pit_raw, drivers),
                    upstream={"provider": "openf1", "base_url": BASE_URL, "meeting_key": meeting["meeting_key"],
                              "session_key": sk, "network_requests": client.network_requests, "cache_hits": client.cache_hits})
    checkpoint()  # last chance: nothing has been written yet
    say("SAVING", f"writing normalized data for {len(drivers)} driver(s)")
    repo.save_session(sid, manifest, laps, weather, telemetry, position)
    return manifest
