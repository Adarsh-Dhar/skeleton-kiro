"""Import one historical session: resolve it on the season schedule, load via FastF1, normalize, store."""

from __future__ import annotations

from typing import Callable

import pandas as pd

from f1lab.integrations.historical import normalizer as N
from f1lab.integrations.historical.fastf1_client import FastF1Client
from f1lab.integrations.historical.repository import HistoricalRepository, session_id, slug

Progress = Callable[[str, str], None]  # (state, detail)

SESSION_CODE = {"FP1": "FP1", "FP2": "FP2", "FP3": "FP3", "Q": "Q", "QUALIFYING": "Q",
                "SQ": "SQ", "SPRINT QUALIFYING": "SQ", "S": "S", "SPRINT": "S", "R": "R", "RACE": "R"}


def _session_code(session: str) -> str:
    code = SESSION_CODE.get(session.strip().upper(), session.strip().upper())
    if code not in ("FP1", "FP2", "FP3", "Q", "SQ", "S", "R"):
        raise ValueError(f"unsupported session '{session}'; use one of FP1, FP2, FP3, Q, SQ, S, R")
    return code


def resolve_event(schedule: pd.DataFrame, event: str) -> str:
    """Map a user-supplied event string to the EventName FastF1 expects."""
    want = event.strip().lower()
    names = schedule["EventName"].astype(str).str.lower().tolist() if "EventName" in schedule.columns else []
    locs = schedule["Location"].astype(str).str.lower().tolist() if "Location" in schedule.columns else []
    for col_vals, exact in ((names, True), (locs, False)):
        for i, v in enumerate(col_vals):
            if exact and v == want:
                return schedule.iloc[i]["EventName"]
    for i, v in enumerate(names):
        if v == want or want in v or v in want:
            return schedule.iloc[i]["EventName"]
    for i, v in enumerate(locs):
        if v == want or want in v or v in want:
            return schedule.iloc[i]["EventName"]
    raise ValueError(f"no Grand Prix matching '{event}' in the season schedule")


def import_session(client: FastF1Client, repo: HistoricalRepository, payload: dict, progress: Progress | None = None) -> dict:
    """Run one import synchronously (call via asyncio.to_thread). Returns the stored manifest."""
    say = progress or (lambda *_: None)
    year = int(payload["year"])
    code = _session_code(str(payload["session"]))

    say("DOWNLOADING", f"loading the {year} season schedule")
    schedule = client.get_schedule(year)
    event_name = resolve_event(schedule, str(payload["event"]))

    say("DOWNLOADING", f"downloading {event_name} {code} (FastF1 cache: {repo.cache_dir})")
    session = client.load_session(year, event_name, code)

    say("PROCESSING", "normalizing laps and session metadata")
    info = N.session_info(session, year, event_name, code)
    drivers_all = N.driver_list(session)
    wanted = [d.strip().upper() for d in payload.get("drivers") or []]
    drivers = [d for d in drivers_all
               if not wanted or d["code"].upper() in wanted or str(d.get("number", "")).upper() in wanted]
    if wanted:
        have = {x["code"].upper() for x in drivers} | {str(x.get("number", "")).upper() for x in drivers}
        missing = [d for d in wanted if d not in have]
        if missing:
            raise ValueError(f"driver(s) not in this session: {', '.join(missing)}")
    if not drivers:
        raise ValueError("no drivers selected")

    laps = N.normalize_laps(session)
    if laps.empty:
        raise ValueError("no lap timing data could be loaded for this session "
                         "(FastF1's timing feed may require F1 account credentials: set F1_USERNAME/F1_PASSWORD)")
    weather = N.normalize_weather(session) if payload.get("include_weather", True) else pd.DataFrame()
    corners = N.circuit_corners(session)

    telemetry: dict[str, pd.DataFrame] = {}
    position: dict[str, pd.DataFrame] = {}
    channels = {"laps": not laps.empty, "weather": not weather.empty, "telemetry": {}, "position": {}}
    for d in drivers:
        code_d = d["code"]
        if payload.get("include_telemetry", True):
            say("PROCESSING", f"extracting car telemetry for {code_d}")
            telemetry[code_d] = N.driver_telemetry(session, code_d, d.get("number"))
            channels["telemetry"][code_d] = not telemetry[code_d].empty
        if payload.get("include_position", True):
            say("PROCESSING", f"extracting track position for {code_d}")
            position[code_d] = N.driver_position(session, code_d, d.get("number"))
            channels["position"][code_d] = not position[code_d].empty

    manifest = N.build_manifest(info, drivers, channels, session_id(year, event_name, code))
    manifest["corners"] = corners
    manifest["upstream"] = {"provider": "fastf1", "year": year, "event": event_name, "session": code,
                            "cache_dir": str(repo.cache_dir)}

    say("SAVING", f"writing normalized data for {len(drivers)} driver(s)")
    repo.save_session(manifest["session_id"], manifest, laps, weather, telemetry, position)
    return manifest
