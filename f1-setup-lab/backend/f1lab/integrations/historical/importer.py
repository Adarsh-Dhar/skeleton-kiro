"""Import one historical session: resolve it on the season schedule, load via FastF1, normalize, store."""

from __future__ import annotations

import socket
from collections.abc import Callable

import pandas as pd

from f1lab.integrations.historical import normalizer as N
from f1lab.integrations.historical.fastf1_client import FastF1Client, FastF1Unavailable, credentials_configured
from f1lab.integrations.historical.repository import HistoricalRepository, session_id

Progress = Callable[[str, str], None]  # (state, detail)

SESSION_CODE = {"FP1": "FP1", "FP2": "FP2", "FP3": "FP3", "Q": "Q", "QUALIFYING": "Q",
                "SQ": "SQ", "SPRINT QUALIFYING": "SQ", "S": "S", "SPRINT": "S", "R": "R", "RACE": "R"}

# error codes reported to the UI/CLI (also stored on the import job)
INVALID_REQUEST = "INVALID_REQUEST"
EVENT_NOT_FOUND = "EVENT_NOT_FOUND"
SESSION_UNAVAILABLE = "SESSION_UNAVAILABLE"
DRIVER_NOT_FOUND = "DRIVER_NOT_FOUND"
NO_LAP_DATA = "NO_LAP_DATA"
NETWORK_TIMEOUT = "NETWORK_TIMEOUT"
NETWORK_ERROR = "NETWORK_ERROR"
CANCELLED = "CANCELLED"
INTERRUPTED = "INTERRUPTED"
INTERNAL = "INTERNAL"


class HistoricalImportError(ValueError):
    """An import failure with a stable ``code`` (see the constants above)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ImportCancelled(Exception):
    pass


def classify(exc: BaseException) -> str:
    """Map any exception raised during an import to an error code."""
    if isinstance(exc, HistoricalImportError):
        return exc.code
    if isinstance(exc, ImportCancelled):
        return CANCELLED
    chain: list[BaseException] = []
    cur: BaseException | None = exc
    while cur is not None and len(chain) < 10:
        chain.append(cur)
        cur = cur.__cause__ or cur.__context__
    names = " ".join(type(e).__name__.lower() for e in chain)
    text = " ".join(str(e).lower() for e in chain)
    if any(isinstance(e, (TimeoutError, socket.timeout)) for e in chain) or "timeout" in names or "timed out" in text or "timeout" in text:
        return NETWORK_TIMEOUT
    if any(isinstance(e, ConnectionError) for e in chain) or "connectionerror" in names or "max retries exceeded" in text \
            or "name or service not known" in text or "nodename nor servname" in text or "failed to resolve" in text:
        return NETWORK_ERROR
    if isinstance(exc, FastF1Unavailable):
        return SESSION_UNAVAILABLE
    if isinstance(exc, ValueError):
        return INVALID_REQUEST
    return INTERNAL


def _session_code(session: str) -> str:
    code = SESSION_CODE.get(session.strip().upper(), session.strip().upper())
    if code not in ("FP1", "FP2", "FP3", "Q", "SQ", "S", "R"):
        raise HistoricalImportError(INVALID_REQUEST, f"unsupported session '{session}'; use one of FP1, FP2, FP3, Q, SQ, S, R")
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
    raise HistoricalImportError(EVENT_NOT_FOUND, f"no Grand Prix matching '{event}' in the season schedule")


def covers(manifest: dict, payload: dict) -> bool:
    """True when a stored session already holds everything ``payload`` asks for (drivers and channels)."""
    stored = {str(d.get("code", "")).upper() for d in manifest.get("drivers", [])} | \
             {str(d.get("number", "")).upper() for d in manifest.get("drivers", [])}
    wanted = {d.strip().upper() for d in payload.get("drivers") or []}
    if not wanted and not manifest.get("all_drivers"):
        return False
    if not wanted <= stored:
        return False
    ch = manifest.get("channels", {})
    codes = [d["code"] for d in manifest.get("drivers", []) if not wanted or str(d.get("code", "")).upper() in wanted
             or str(d.get("number", "")).upper() in wanted]
    if payload.get("include_telemetry", True) and any(c not in ch.get("telemetry", {}) for c in codes):
        return False
    if payload.get("include_position", True) and any(c not in ch.get("position", {}) for c in codes):
        return False
    if payload.get("include_weather", True) and "weather" not in ch:
        return False
    return True


def import_session(client: FastF1Client, repo: HistoricalRepository, payload: dict, progress: Progress | None = None, *,
                   should_cancel: Callable[[], bool] | None = None, force: bool = True) -> dict:
    """Run one import synchronously (call via asyncio.to_thread). Returns the stored manifest.

    ``should_cancel`` is polled between steps (FastF1's own download cannot be interrupted midway); a cancelled import
    stores nothing. With ``force=False`` an already stored session that covers the request is reused without
    downloading telemetry again (``manifest['reused'] = True``).
    """
    say = progress or (lambda *_: None)

    def checkpoint() -> None:
        if should_cancel and should_cancel():
            raise ImportCancelled("import cancelled")

    year = int(payload["year"])
    code = _session_code(str(payload["session"]))

    say("DOWNLOADING", f"loading the {year} season schedule")
    schedule = client.get_schedule(year)
    if schedule is None or len(schedule) == 0:
        raise HistoricalImportError(SESSION_UNAVAILABLE, f"no {year} season schedule is available")
    event_name = resolve_event(schedule, str(payload["event"]))
    sid = session_id(year, event_name, code)
    checkpoint()

    if not force:
        existing = repo.manifest(sid)
        if existing is not None and covers(existing, payload):
            say("COMPLETED", f"{event_name} {code} is already stored; reusing it (no download)")
            return {**existing, "reused": True}

    say("DOWNLOADING", f"downloading {event_name} {code} (FastF1 cache: {repo.cache_dir})")
    try:
        session = client.load_session(year, event_name, code)
    except FastF1Unavailable as exc:
        kind = classify(exc)
        raise HistoricalImportError(kind if kind in (NETWORK_TIMEOUT, NETWORK_ERROR) else SESSION_UNAVAILABLE, str(exc)) from exc
    checkpoint()

    say("PROCESSING", "normalizing laps and session metadata")
    info = N.session_info(session, year, event_name, code)
    drivers_all = N.driver_list(session)
    if not drivers_all:
        raise HistoricalImportError(SESSION_UNAVAILABLE, f"{event_name} {code} has no driver list (session not held or data not published)")
    wanted = [d.strip().upper() for d in payload.get("drivers") or []]
    drivers = [d for d in drivers_all
               if not wanted or d["code"].upper() in wanted or str(d.get("number", "")).upper() in wanted]
    if wanted:
        have = {x["code"].upper() for x in drivers} | {str(x.get("number", "")).upper() for x in drivers}
        missing = [d for d in wanted if d not in have]
        if missing:
            raise HistoricalImportError(DRIVER_NOT_FOUND, f"driver(s) not in this session: {', '.join(missing)}")
    if not drivers:
        raise HistoricalImportError(DRIVER_NOT_FOUND, "no drivers selected")

    laps = N.normalize_laps(session)
    if laps.empty:
        hint = ("no F1 account credentials are configured: FastF1 needs F1_USERNAME and F1_PASSWORD in the environment of the app "
                "(never in code or files you share)" if not credentials_configured() else
                "F1 account credentials are configured; the timing feed for this session may not be published")
        raise HistoricalImportError(NO_LAP_DATA, f"no lap timing data could be loaded for this session ({hint})")
    weather = N.normalize_weather(session) if payload.get("include_weather", True) else pd.DataFrame()
    corners = N.circuit_corners(session)

    telemetry: dict[str, pd.DataFrame] = {}
    position: dict[str, pd.DataFrame] = {}
    warnings: list[str] = []
    channels: dict = {"laps": not laps.empty, "telemetry": {}, "position": {}}
    if payload.get("include_weather", True):
        channels["weather"] = not weather.empty
        if weather.empty:
            warnings.append("no weather samples in this session")
    if not corners:
        warnings.append("no circuit corner data (FastF1 circuit info unavailable)")
    for d in drivers:
        checkpoint()
        code_d = d["code"]
        if payload.get("include_telemetry", True):
            say("PROCESSING", f"extracting car telemetry for {code_d}")
            telemetry[code_d] = N.driver_telemetry(session, code_d, d.get("number"))
            channels["telemetry"][code_d] = not telemetry[code_d].empty
            if telemetry[code_d].empty:
                warnings.append(f"no car telemetry for {code_d}")
        if payload.get("include_position", True):
            say("PROCESSING", f"extracting track position for {code_d}")
            position[code_d] = N.driver_position(session, code_d, d.get("number"))
            channels["position"][code_d] = not position[code_d].empty
            if position[code_d].empty:
                warnings.append(f"no track position for {code_d}")

    manifest = N.build_manifest(info, drivers, channels, sid)
    manifest["corners"] = corners
    manifest["all_drivers"] = not wanted
    manifest["warnings"] = warnings
    manifest["partial"] = any(w.startswith(("no car telemetry", "no track position")) for w in warnings)
    manifest["upstream"] = {"provider": "fastf1", "year": year, "event": event_name, "session": code,
                            "cache_dir": str(repo.cache_dir)}

    checkpoint()  # last chance: nothing has been written yet
    say("SAVING", f"writing normalized data for {len(drivers)} driver(s)")
    repo.save_session(manifest["session_id"], manifest, laps, weather, telemetry, position)
    return manifest
