"""Import one historical session from OpenF1 into the same normalized storage the FastF1 importer uses."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

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


RECENT_SESSION = timedelta(hours=48)  # a session this fresh may still be gaining data: cached answers are re-checked after an hour
RECENT_MAX_AGE_S = 3600.0
RACE_CODES = ("R", "S")


def meetings_for(client: OpenF1Client, year: int, cancel=None, **kw) -> list[dict]:
    check_year(year)
    rows = client.get("meetings", {"year": year}, cancel=cancel, **kw)
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
    if payload.get("include_weather", True) and "weather" not in ch:
        return False
    legacy = set(N.OPTIONAL_DATASETS) if manifest.get("race_data_included") else set(N.STRATEGY_DATASETS)  # manifests written before dataset selection
    return N.requested_datasets(payload) <= set(manifest.get("requested_datasets") or legacy)


def _chunks(start, end):
    cur = start
    while cur < end:
        nxt = min(cur + CHUNK, end)
        yield cur, nxt
        cur = nxt


def import_session_openf1(client: OpenF1Client, repo: HistoricalRepository, payload: dict, progress: Progress | None = None, *,
                          should_cancel: Callable[[], bool] | None = None, force: bool = True) -> dict:
    """Run one OpenF1 import synchronously (call via asyncio.to_thread). Returns the stored manifest.

    The lap table is required. Everything else (weather, stints, pit stops, race control, running order, results, grid, gaps,
    overtakes, team radio, championships, and each driver's car/location data) is optional: when one is empty or fails the
    session is still stored and ``coverage`` records, per endpoint, the rows, requests, cache hits and when it was fetched.
    ``partial`` is set when an endpoint failed or a driver is missing car/location data (an empty beta or team-radio endpoint
    is reported as ``empty``, not as a failure). Cancelling, or a failure of a required step, stores nothing.

    ``payload['refresh_cache']`` skips the OpenF1 disk cache for every request of this import.
    """
    say = progress or (lambda *_: None)
    refresh = bool(payload.get("refresh_cache"))
    state = {"max_age": None}

    def cancelled() -> bool:
        return bool(should_cancel and should_cancel())

    def checkpoint() -> None:
        if cancelled():
            raise ImportCancelled("import cancelled")

    year = int(payload["year"])
    check_year(year)
    code = _session_code(str(payload["session"]))
    coverage: dict = {"endpoints": {}, "drivers_empty": {"car_data": [], "location": []}, "requested_at": datetime.now(UTC).isoformat(timespec="seconds"),
                      "refresh_cache": refresh}
    warnings: list[str] = []
    partial = False

    def call(name: str, endpoint: str, filters: dict, ranges=()) -> list[dict]:
        """One OpenF1 request, recorded in the coverage entry ``name``."""
        ent = coverage["endpoints"].setdefault(name, {"endpoint": endpoint, "rows": 0, "status": "empty", "requests": 0, "cache_hits": 0, "fetched_at": None})
        try:
            rows = client.get(endpoint, filters, ranges, cancel=cancelled, refresh=refresh, max_age_s=state["max_age"])
        except OpenF1Error as exc:
            ent.update(status="error", note=str(exc))
            raise
        meta = getattr(client, "last_meta", {}) or {}
        ent["rows"] += len(rows)
        ent["requests"] += 1
        ent["cache_hits"] += 1 if meta.get("from_cache") else 0
        if meta.get("fetched_at") and (ent["fetched_at"] is None or meta["fetched_at"] < ent["fetched_at"]):
            ent["fetched_at"] = meta["fetched_at"]  # the oldest answer used
        if ent["status"] != "error":
            ent["status"] = "ok" if ent["rows"] else "empty"
        return rows

    def optional(name: str, endpoint: str, filters: dict) -> list[dict]:
        nonlocal partial
        try:
            return call(name, endpoint, filters)
        except OpenF1Error as exc:
            warnings.append(f"{name} unavailable: {exc}")
            partial = True
            return []

    say("DOWNLOADING", f"loading the {year} OpenF1 meetings")
    meetings = client.get("meetings", {"year": year}, cancel=cancelled, refresh=refresh)
    if not N.schedule_frame(meetings).shape[0]:
        raise HistoricalImportError(SESSION_UNAVAILABLE, f"OpenF1 has no {year} meetings")
    meeting = resolve_meeting(meetings, str(payload["event"]))
    sid = session_id(year, meeting["meeting_name"], code, "openf1")
    checkpoint()

    if not force:
        existing = repo.manifest(sid)
        if existing is not None and covers(existing, payload):
            say("COMPLETED", f"{meeting['meeting_name']} {code} is already stored; reusing it (no download)")
            return {**existing, "reused": True}

    meeting_sessions = client.get("sessions", {"meeting_key": meeting["meeting_key"]}, cancel=cancelled, refresh=refresh)
    sess = pick_session(meeting_sessions, code)
    sk = sess["session_key"]
    end = N.parse_dt(sess.get("date_end")) or (N.parse_dt(sess.get("date_start")) or datetime.now(UTC)) + timedelta(hours=6)
    if datetime.now(UTC) - end < RECENT_SESSION:
        state["max_age"] = RECENT_MAX_AGE_S  # may still be publishing: do not trust old cached answers
        warnings.append("this session finished within the last 48 hours; OpenF1 may still be publishing data, re-import later for a complete set")
    say("DOWNLOADING", f"downloading {meeting['meeting_name']} {code} from OpenF1 (cache: {client.cache_dir})")
    drivers_all = N.driver_list(call("drivers", "drivers", {"session_key": sk}))
    if not drivers_all:
        raise HistoricalImportError(SESSION_UNAVAILABLE, f"{meeting['meeting_name']} {code} has no driver list in OpenF1 (session not held or data not published)")
    wanted = [d.strip().upper() for d in payload.get("drivers") or []]
    drivers = select_drivers(drivers_all, wanted)
    checkpoint()

    laps_raw = call("laps", "laps", {"session_key": sk})
    if not laps_raw:
        raise HistoricalImportError(NO_LAP_DATA, "OpenF1 has no lap timing data for this session (it may not be published yet)")
    checkpoint()
    asked = N.requested_datasets(payload)
    stints_raw = optional("stints", "stints", {"session_key": sk}) if "stints" in asked else []
    pit_raw = optional("pit", "pit", {"session_key": sk}) if "pit" in asked else []
    rc_raw = optional("race_control", "race_control", {"session_key": sk}) if "race_control" in asked else []
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

    # ---- race data: running order, results, grid, gaps, overtakes, radio, championships (all optional)
    datasets: dict[str, tuple[list[dict], str | None]] = {}
    include_race = bool(payload.get("include_race_data", True)) and bool(asked & set(N.RACE_DATASETS))
    if include_race:
        say("PROCESSING", "downloading running order, results, grid, gaps and radio")
        by = {"session_key": sk}
        plan = [("race_position", "position", by, lambda r: N.race_position(r, drivers_all, t0), None),
                ("session_result", "session_result", by, lambda r: N.session_result(r, drivers_all), None),
                ("team_radio", "team_radio", by, lambda r: N.team_radio(r, drivers_all, t0), "OpenF1 publishes only a limited selection of radio clips")]
        if code in RACE_CODES:
            gs = N.grid_session(meeting_sessions, code)
            plan += [("starting_grid", "starting_grid", {"session_key": gs["session_key"]} if gs else None, lambda r: N.starting_grid(r, drivers_all),
                      "OpenF1 files the grid under the qualifying session that set it"),
                     ("intervals", "intervals", by, lambda r: N.intervals(r, drivers_all, t0), None),
                     ("overtakes", "overtakes", by, lambda r: N.overtakes(r, drivers_all, t0), None),
                     ("drivers_championship", "drivers_championship", by, lambda r: N.drivers_championship(r, drivers_all), "beta endpoint"),
                     ("teams_championship", "teams_championship", by, lambda r: N.teams_championship(r), "beta endpoint")]
        else:
            for name in (n for n in ("starting_grid", *N.RACE_ONLY) if n in asked):
                coverage["endpoints"][name] = {"endpoint": name, "rows": 0, "status": "not_applicable", "requests": 0, "cache_hits": 0, "fetched_at": None,
                                               "note": "only published for race and sprint sessions"}
        plan = [p for p in plan if p[0] in asked]
        for name, endpoint, filters, norm, note in plan:
            checkpoint()
            if filters is None:
                coverage["endpoints"][name] = {"endpoint": endpoint, "rows": 0, "status": "empty", "requests": 0, "cache_hits": 0, "fetched_at": None,
                                               "note": "no qualifying session found for this weekend"}
                continue
            rows = optional(name, endpoint, filters)
            ent = coverage["endpoints"][name]
            if note:
                ent.setdefault("note", note)
            if endpoint in N.BETA_ENDPOINTS and not rows and ent["status"] == "empty":
                ent["note"] = "beta endpoint: OpenF1 published no data for this session"
            datasets[name] = (norm(rows), ent["fetched_at"])
        checkpoint()

    # ---- per-driver car data and location, in 30-minute windows
    def per_driver(endpoint: str, d: dict, builder) -> pd.DataFrame:
        nonlocal partial
        number = int(d["number"])
        wins = windows.get(number, [])
        coverage["endpoints"].setdefault(endpoint, {"endpoint": endpoint, "rows": 0, "status": "empty", "requests": 0, "cache_hits": 0, "fetched_at": None})
        if not wins:
            coverage["drivers_empty"][endpoint].append(d["code"])
            warnings.append(f"no {endpoint} for {d['code']} (no lap start times to anchor it)")
            partial = True
            return pd.DataFrame()
        rows: list[dict] = []
        try:
            for a, b in _chunks(min(w[1] for w in wins), max(w[2] for w in wins)):
                rows += call(endpoint, endpoint, {"session_key": sk, "driver_number": number},
                             (("date", ">=", N.api_time(a)), ("date", "<", N.api_time(b, ceil=True))))
        except OpenF1Error as exc:
            warnings.append(f"{endpoint} for {d['code']} failed: {exc}")
            coverage["drivers_empty"][endpoint].append(d["code"])
            partial = True
            return pd.DataFrame()
        df = builder(rows, wins, t0)
        if df.empty:
            coverage["drivers_empty"][endpoint].append(d["code"])
            warnings.append(f"no {'car telemetry' if endpoint == 'car_data' else 'track location'} for {d['code']}")
            partial = True
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
            say("PROCESSING", f"downloading track location for {d['code']}")
            position[d["code"]] = per_driver("location", d, N.location_frame)
            channels["position"][d["code"]] = not position[d["code"]].empty
    for name, ent in coverage["endpoints"].items():
        if ent["status"] == "ok" and not ent["rows"]:
            ent["status"] = "empty"
        if name in ("car_data", "location"):
            ent["status"] = "error" if ent["status"] == "error" else ("ok" if ent["rows"] else "empty")

    info = N.session_info(meeting, sess, year, code, N.round_number(meetings, meeting))
    manifest = N.build_manifest(info, drivers, channels, sid)
    manifest.update(corners=[], all_drivers=not wanted, warnings=warnings, coverage=coverage, partial=partial,
                    race_control=N.race_control(rc_raw, t0, drivers_all), pit_stops=N.pit_stops(pit_raw, drivers), race_data_included=include_race, requested_datasets=sorted(asked),
                    datasets={n: {"rows": len(r), "fetched_at": f} for n, (r, f) in datasets.items()},
                    upstream={"provider": "openf1", "base_url": BASE_URL, "meeting_key": meeting["meeting_key"], "session_key": sk,
                              "network_requests": client.network_requests, "cache_hits": client.cache_hits})
    checkpoint()  # last chance: nothing has been written yet
    say("SAVING", f"writing normalized data for {len(drivers)} driver(s)")
    repo.save_session(sid, manifest, laps, weather, telemetry, position, datasets)
    return manifest
