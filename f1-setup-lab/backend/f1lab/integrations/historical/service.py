"""HistoricalDataService: coordinates FastF1 imports (background jobs) and session queries.

Historical sessions and import jobs live in the root database (``f1_hist_*``, ``f1_import_job``) and are completely
separate from the TRIALFORGE experiment tables: they are for analysis/benchmarking, never setup-response measurements.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import Engine

from f1lab.integrations.historical import openf1_importer, openf1_normalizer
from f1lab.integrations.historical.fastf1_client import FastF1Client, cache_stats
from f1lab.integrations.historical.importer import (
    CANCELLED,
    NETWORK_TIMEOUT,
    ImportCancelled,
    _session_code,
    classify,
    import_session,
    resolve_event,
)
from f1lab.integrations.historical.jobs import ACTIVE, ImportJobStore, dedupe_key
from f1lab.integrations.historical.openf1_client import OpenF1Client
from f1lab.integrations.historical.openf1_client import cache_stats as openf1_cache_stats
from f1lab.integrations.historical.openf1_importer import import_session_openf1
from f1lab.integrations.historical.repository import HistoricalRepository
from f1lab.integrations.historical.schemas import HistoricalCompareRequest, HistoricalImportRequest

TELEMETRY_CHANNELS = ("distance_m", "session_time_s", "speed_kph", "throttle_pct", "brake_applied", "brake_pct",
                      "steer_pct", "gear", "rpm", "drs")
DATASETS = ("race_position", "session_result", "starting_grid", "intervals", "overtakes", "team_radio",
            "drivers_championship", "teams_championship")
MANIFEST_DATASETS = ("pit_stops", "race_control")  # small lists kept in the session manifest
POSITION_CHANNELS = ("distance_m", "session_time_s", "x_m", "y_m", "z_m", "speed_kph")

# FastF1 schedule session names -> session type codes accepted by get_session()
SESSION_NAME_TO_TYPE = {
    "practice 1": "FP1", "practice 2": "FP2", "practice 3": "FP3",
    "qualifying": "Q", "sprint qualifying": "SQ", "sprint": "S", "race": "R",
}


def _schedule_sessions(row) -> list[dict]:
    """One schedule row -> [{type, name, date}] for each non-empty SessionN column."""
    out = []
    for i in range(1, 6):
        name = row.get(f"Session{i}")
        if name is None or (isinstance(name, float) and pd.isna(name)) or str(name).strip() == "":
            continue
        code = SESSION_NAME_TO_TYPE.get(str(name).strip().lower())
        date = row.get(f"Session{i}Date")
        out.append({"type": code, "name": str(name), "date": str(date) if date is not None and not pd.isna(date) else ""})
    return out


def _thin(df: pd.DataFrame, limit: int) -> pd.DataFrame:
    """Evenly-spaced downsampling that preserves the trace shape."""
    if df is None or df.empty or len(df) <= limit:
        return df
    idx = np.unique(np.linspace(0, len(df) - 1, limit).round().astype(int))
    return df.iloc[idx]


def _records(df: pd.DataFrame, limit: int | None = None, channels: tuple[str, ...] | None = None) -> list[dict]:
    if df is None or df.empty:
        return []
    if channels:
        df = df[[c for c in channels if c in df.columns]]
    if limit:
        df = _thin(df, limit)
    return HistoricalRepository.records(df)


class HistoricalService:
    """``engine`` is the root database; ``cache_dir`` is FastF1's download cache (reused by every import).
    OpenF1 responses are cached next to it in ``openf1-cache``."""

    def __init__(self, engine: Engine, cache_dir: str | Path, client_factory: Callable[[], Any] | None = None,
                 job_timeout_s: float = 1800.0, openf1_client_factory: Callable[[], Any] | None = None) -> None:
        self.repo = HistoricalRepository(engine, cache_dir)
        self.openf1_cache_dir = Path(cache_dir).parent / "openf1-cache"
        self.jobs = ImportJobStore(engine)
        self._client: FastF1Client | None = None
        self._client_factory = client_factory
        self._openf1: OpenF1Client | None = None
        self._openf1_factory = openf1_client_factory
        self._driver_cache: dict[tuple, list[dict]] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._gate = asyncio.Semaphore(1)  # one FastF1 import at a time (FastF1 is not designed for concurrent loads)
        self.job_timeout_s = job_timeout_s

    def recover(self) -> int:
        """Mark imports interrupted by a previous shutdown/crash as FAILED (code INTERRUPTED). Returns how many."""
        return self.jobs.mark_interrupted()

    async def shutdown(self) -> None:
        for jid, task in list(self._tasks.items()):
            if not task.done():
                self.jobs.request_cancel(jid)
                task.cancel()
        self._tasks.clear()

    @property
    def client(self) -> FastF1Client:
        if self._client is None:
            self._client = self._client_factory() if self._client_factory else FastF1Client(self.repo.cache_dir)
        return self._client

    @property
    def openf1(self) -> OpenF1Client:
        if self._openf1 is None:
            self._openf1 = self._openf1_factory() if self._openf1_factory else OpenF1Client(self.openf1_cache_dir)
        return self._openf1

    # ------------------------------------------------------------- schedule browsing
    def events(self, year: int, provider: str = "fastf1") -> list[dict]:
        if provider == "openf1":
            openf1_importer.check_year(year)
            meetings = self.openf1.get("meetings", {"year": year})
            if not openf1_normalizer.schedule_frame(meetings).shape[0]:
                return []  # a season OpenF1 has not published yet is an empty list, not an error
            return openf1_normalizer.event_list(meetings, self.openf1.get("sessions", {"year": year}))
        schedule = self.client.get_schedule(year)
        if schedule is None or schedule.empty:
            return []
        out = []
        for _, row in schedule.iterrows():
            out.append({
                "event": row.get("EventName"),
                "official_event": row.get("OfficialEventName"),
                "location": row.get("Location"),
                "circuit": row.get("Location"),
                "country": row.get("Country"),
                "round": row.get("RoundNumber"),
                "sessions": _schedule_sessions(row),
            })
        return out

    def sessions_for_event(self, year: int, event: str, provider: str = "fastf1") -> list[dict]:
        if provider == "openf1":
            meeting = openf1_importer.resolve_meeting(openf1_importer.meetings_for(self.openf1, year), event)
            return openf1_normalizer.schedule_sessions(self.openf1.get("sessions", {"meeting_key": meeting["meeting_key"]}))
        schedule = self.client.get_schedule(year)
        name = resolve_event(schedule, event)
        row = schedule[schedule["EventName"] == name].iloc[0]
        return _schedule_sessions(row)

    async def drivers_preview(self, year: int, event: str, session: str, provider: str = "fastf1") -> list[dict]:
        """Driver list for a schedule session, without a full telemetry import (laps only)."""
        code = _session_code(session)
        key = (provider, year, event.strip().lower(), code)
        if key in self._driver_cache:
            return self._driver_cache[key]
        if provider == "openf1":
            drivers = await asyncio.to_thread(openf1_importer.preview, self.openf1, year, event, session)
            self._driver_cache[key] = drivers
            return drivers
        schedule = await asyncio.to_thread(self.client.get_schedule, year)
        event_name = await asyncio.to_thread(resolve_event, schedule, event)
        sess = await asyncio.to_thread(
            self.client.load_session, year, event_name, code,
            laps=True, telemetry=False, weather=False, messages=False, circuit_info=False)
        from f1lab.integrations.historical import normalizer as N
        drivers = N.driver_list(sess)
        self._driver_cache[key] = drivers
        return drivers

    # ------------------------------------------------------------- imports
    @staticmethod
    def _not_found(what: str):
        from f1lab.services.lab import NotFound

        return NotFound(what)

    async def start_import(self, req: HistoricalImportRequest, force: bool = False) -> dict:
        """Queue an import. A request identical to one already running, or one already completed whose session is
        still stored, returns that job (``duplicate: true``) instead of downloading again, unless ``force``."""
        payload = req.model_dump()
        force = force or req.refresh_cache  # a cache refresh always runs again
        key = dedupe_key(payload)
        active = self.jobs.find_active(key)
        if active is not None:
            return {**active, "duplicate": True}
        if not force:
            done = self.jobs.find_completed(key)
            if done is not None and done.get("session_id") and self.repo.exists(done["session_id"]):
                return {**done, "duplicate": True}
        job = self.jobs.create(payload)
        self._spawn(job["import_id"], payload, force)
        return {**job, "duplicate": False}

    def _spawn(self, import_id: str, payload: dict, force: bool) -> None:
        task = asyncio.create_task(self._run_import(import_id, payload, force))
        self._tasks[import_id] = task
        task.add_done_callback(lambda _t, i=import_id: self._tasks.pop(i, None))

    async def _run_import(self, import_id: str, payload: dict, force: bool) -> None:
        jobs = self.jobs
        if self._gate.locked():
            jobs.progress(import_id, "QUEUED", "waiting for another import to finish")
        async with self._gate:
            if jobs.cancel_requested(import_id):
                jobs.update(import_id, state="CANCELLED", error_code=CANCELLED, error="cancelled before it started", detail="cancelled")
                return
            timed_out = False

            def progress(state: str, detail: str) -> None:
                jobs.progress(import_id, state if state in ACTIVE else "PROCESSING", detail)

            def should_cancel() -> bool:
                return timed_out or jobs.cancel_requested(import_id)

            openf1 = payload.get("provider", "fastf1") == "openf1"
            stats = openf1_cache_stats if openf1 else cache_stats
            cache_dir = self.openf1_cache_dir if openf1 else self.repo.cache_dir
            label = "OpenF1" if openf1 else "FastF1"
            before = stats(cache_dir)
            run, client = (import_session_openf1, self.openf1) if openf1 else (import_session, self.client)
            work = asyncio.to_thread(run, client, self.repo, payload, progress, should_cancel=should_cancel, force=force)
            try:
                manifest = await asyncio.wait_for(asyncio.shield(work), timeout=self.job_timeout_s)
            except TimeoutError:
                timed_out = True  # the worker thread stops at its next checkpoint and stores nothing
                jobs.update(import_id, state="FAILED", error_code=NETWORK_TIMEOUT,
                            error=f"the import did not finish within {int(self.job_timeout_s)} s; nothing was stored. Retry it.",
                            detail="timed out")
                return
            except asyncio.CancelledError:
                jobs.update(import_id, state="CANCELLED", error_code=CANCELLED, error="the application shut down", detail="cancelled")
                raise
            except ImportCancelled:
                jobs.update(import_id, state="CANCELLED", error_code=CANCELLED, error="cancelled by the user; nothing was stored", detail="cancelled")
                return
            except Exception as exc:  # network errors, missing data, bad driver codes
                jobs.update(import_id, state="FAILED", error_code=classify(exc), error=str(exc) or type(exc).__name__, detail="failed")
                return
            after = stats(cache_dir)
            if manifest.get("reused"):
                note = "already stored; reused without downloading"
            elif after["bytes"] > before["bytes"]:
                note = f"import complete; downloaded {round((after['bytes'] - before['bytes']) / 1e6, 1)} MB into the {label} cache"
            else:
                note = f"import complete; served from the {label} cache (no new downloads)"
            if manifest.get("warnings"):
                note += "; warnings: " + "; ".join(manifest["warnings"][:5])
            jobs.update(import_id, state="COMPLETED", detail=note, session_id=manifest["session_id"], error=None, error_code=None)

    def import_status(self, import_id: str) -> dict:
        job = self.jobs.get(import_id)
        if job is None:
            raise self._not_found(import_id)
        return job

    def list_imports(self, limit: int = 50) -> list[dict]:
        return self.jobs.list(limit)

    def cancel_import(self, import_id: str) -> dict:
        job = self.import_status(import_id)
        if job["state"] not in ACTIVE:
            from f1lab.services.lab import Conflict

            raise Conflict(f"import {import_id} is {job['state']}; only queued or running imports can be cancelled")
        self.jobs.request_cancel(import_id)
        if import_id not in self._tasks:  # not running in this process (should not happen after recover())
            self.jobs.update(import_id, state="CANCELLED", error_code=CANCELLED, error="cancelled", detail="cancelled")
        else:
            self.jobs.progress(import_id, job["state"], "cancelling: stops at the next step (an in-flight download finishes first)")
        return self.import_status(import_id)

    async def retry_import(self, import_id: str) -> dict:
        job = self.import_status(import_id)
        if job["state"] not in ("FAILED", "CANCELLED"):
            from f1lab.services.lab import Conflict

            raise Conflict(f"import {import_id} is {job['state']}; only failed or cancelled imports can be retried")
        active = self.jobs.find_active(dedupe_key(job["request"]))
        if active is not None:
            return {**active, "duplicate": True}
        new = self.jobs.create(job["request"], retry_of=import_id, attempts=job["attempts"] + 1)
        self._spawn(new["import_id"], job["request"], False)
        return {**new, "duplicate": False}

    def delete_session(self, sid: str) -> dict:
        if not self.repo.delete_session(sid):
            raise self._not_found(sid)
        return {"deleted": sid}

    # ------------------------------------------------------------- stored sessions
    def _manifest(self, sid: str) -> dict:
        m = self.repo.manifest(sid)
        if m is None:
            raise self._not_found(sid)
        return m

    def list_sessions(self) -> list[dict]:
        return self.repo.list_sessions()

    def get_session(self, sid: str) -> dict:
        return self._manifest(sid)

    def dataset(self, sid: str, name: str, driver: str | None = None, limit: int = 4000) -> dict:
        """One stored per-session dataset (running order, results, grid, gaps, overtakes, radio, championships, pit stops, race control)
        with its coverage entry, so an empty list can be told apart from "never requested" or "failed"."""
        m = self._manifest(sid)
        if name not in DATASETS + MANIFEST_DATASETS:
            raise ValueError(f"unknown dataset '{name}'; choose one of {', '.join(DATASETS + MANIFEST_DATASETS)}")
        rows = list(m.get(name, [])) if name in MANIFEST_DATASETS else self.repo.dataset(sid, name)
        cov_key = {"pit_stops": "pit", "race_control": "race_control"}.get(name, name)
        entry = (m.get("coverage") or {}).get("endpoints", {}).get(cov_key)
        if driver:
            want = driver.strip().upper()
            by_number = {str(d.get("number", "")).upper(): str(d.get("code", "")).upper() for d in m.get("drivers", [])}
            want = by_number.get(want, want)
            rows = [r for r in rows if want in {str(r.get(k) or "").upper() for k in ("driver", "overtaking", "overtaken")}]
        total = len(rows)
        if limit and total > limit:  # evenly thin each driver's series so charts stay light
            groups: dict[str, list[dict]] = {}
            for r in rows:
                groups.setdefault(str(r.get("driver")), []).append(r)
            per = max(50, limit // max(len(groups), 1))
            rows = sorted((r for g in groups.values() for r in (g if len(g) <= per else [g[i] for i in np.unique(np.linspace(0, len(g) - 1, per).round().astype(int))])),
                          key=lambda r: (r.get("date") or "", str(r.get("driver"))))
        available = bool(total) or (entry is not None and entry.get("status") in ("ok",))
        return {"name": name, "rows": rows, "count": total, "returned": len(rows), "thinned": len(rows) < total, "available": available,
                "coverage": entry, "provider": m.get("provider")}

    def openf1_cache(self) -> dict:
        return {**openf1_cache_stats(self.openf1_cache_dir), "ttl_days": self.openf1.ttl_s / 86400.0}

    def clear_openf1_cache(self) -> dict:
        return {"cleared": self.openf1.clear_cache(), **openf1_cache_stats(self.openf1_cache_dir)}

    def drivers(self, sid: str) -> list[dict]:
        return self._manifest(sid).get("drivers", [])

    def _driver_entry(self, sid: str, driver: str) -> dict:
        """Resolve a driver query param to the manifest driver entry (case-insensitive code or number)."""
        m = self._manifest(sid)
        want = driver.strip().upper()
        for d in m.get("drivers", []):
            if str(d.get("code", "")).upper() == want or str(d.get("number", "")).upper() == want:
                return d
        raise ValueError(f"driver '{driver}' is not part of session {sid}")

    def _driver_code(self, sid: str, driver: str) -> str:
        return str(self._driver_entry(sid, driver)["code"])

    def laps(self, sid: str, driver: str | None = None) -> list[dict]:
        df = self.repo.laps(sid)
        if df.empty:
            return []
        if driver:
            d = self._driver_entry(sid, driver)
            keys = {str(d.get("code", "")), str(d.get("number", ""))} - {""}
            df = df[df["driver"].astype(str).isin(keys)]
        return _records(df)

    def telemetry(self, sid: str, driver: str, lap: int | None = None, limit: int = 1500) -> dict:
        code = self._driver_code(sid, driver)
        df = self.repo.telemetry(sid, code)
        if df.empty:
            return {"samples": [], "count": 0, "driver": code, "lap": lap}
        if lap is not None:
            df = df[df["lap_number"] == lap]
        if df.empty:
            return {"samples": [], "count": 0, "driver": code, "lap": lap}
        return {"samples": _records(df, limit, TELEMETRY_CHANNELS), "count": int(len(df)),
                "driver": code, "lap": lap, "interpolated": False}

    def position(self, sid: str, driver: str, lap: int | None = None, limit: int = 3000) -> dict:
        code = self._driver_code(sid, driver)
        df = self.repo.position(sid, code)
        if df.empty:
            return {"points": [], "count": 0, "driver": code, "lap": lap}
        if lap is not None:
            df = df[df["lap_number"] == lap]
        if df.empty:
            return {"points": [], "count": 0, "driver": code, "lap": lap}
        return {"points": _records(df, limit, POSITION_CHANNELS), "count": int(len(df)),
                "driver": code, "lap": lap}

    def weather(self, sid: str) -> dict:
        self._manifest(sid)  # 404 for an unknown session
        df = self.repo.weather(sid)
        return {"samples": _records(df), "count": len(df), "note": "weather is sampled sparsely by the timing feed"}

    def track_map(self, sid: str, driver: str | None = None, lap: int | None = None) -> dict:
        m = self._manifest(sid)
        out: dict[str, Any] = {"corners": m.get("corners", []), "circuit": m.get("circuit")}
        if driver:
            code = self._driver_code(sid, driver)
            pos = self.position(sid, code, lap)
            out.update(driver=code, lap=lap, points=pos["points"][:5000], count=pos["count"])
        return out

    # ------------------------------------------------------------- comparison
    def compare(self, sid: str, req: HistoricalCompareRequest) -> dict:
        """Speed/throttle/brake/etc. overlays plus an interpolated time delta on a common distance grid."""
        a_code, b_code = self._driver_code(sid, req.driver_a), self._driver_code(sid, req.driver_b)
        ta = self.repo.telemetry(sid, a_code)
        tb = self.repo.telemetry(sid, b_code)
        ta = ta[ta["lap_number"] == req.lap_a].sort_values("distance_m") if not ta.empty else ta
        tb = tb[tb["lap_number"] == req.lap_b].sort_values("distance_m") if not tb.empty else tb
        if ta.empty or tb.empty:
            raise ValueError("telemetry missing for one of the selected laps (re-import with telemetry enabled)")

        n = req.points
        # normalize each lap's distance to 0..1 of its own lap distance, interpolate session time
        def _norm(df):
            d = df["distance_m"].astype(float).to_numpy()
            t = df["session_time_s"].astype(float).to_numpy()
            span = d.max() - d.min()
            if span <= 0:
                return np.zeros(n), np.zeros(n)
            grid = np.linspace(0.0, 1.0, n)
            dn = (d - d.min()) / span
            order = np.argsort(dn)
            return grid, np.interp(grid, dn[order], t[order])

        grid, t_a = _norm(ta)
        _, t_b = _norm(tb)
        # express both on a common absolute-ish time base: shift each lap's t to start at 0
        t_a = t_a - t_a[0]
        t_b = t_b - t_b[0]
        delta = t_b - t_a  # positive => B is slower than A at that point of the lap

        def trace(df, limit=600):
            return _records(_thin(df, limit), None, ("distance_m", "speed_kph", "throttle_pct", "brake_applied", "brake_pct", "rpm", "gear", "drs"))

        return {
            "a": {"driver": a_code, "lap": req.lap_a, "points": trace(ta)},
            "b": {"driver": b_code, "lap": req.lap_b, "points": trace(tb)},
            "delta": {"distance_pct": [round(float(x), 4) for x in grid],
                      "delta_s": [round(float(x), 4) for x in delta],
                      "note": "interpolated on a common normalized distance grid; t_B - t_A, positive = B slower"},
        }
