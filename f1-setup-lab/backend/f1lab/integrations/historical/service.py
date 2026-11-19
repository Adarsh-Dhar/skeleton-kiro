"""HistoricalDataService: coordinates FastF1 imports (background jobs) and session queries.

Historical sessions live under ``<data_dir>/historical`` and are completely separate from the
TRIALFORGE experiment store: they are for analysis/benchmarking, never setup-response measurements.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from f1lab.integrations.historical.fastf1_client import FastF1Client, FastF1Unavailable
from f1lab.integrations.historical.importer import import_session, resolve_event, _session_code
from f1lab.integrations.historical.repository import HistoricalRepository, session_id
from f1lab.integrations.historical.schemas import HistoricalCompareRequest, HistoricalImportRequest

TELEMETRY_CHANNELS = ("distance_m", "session_time_s", "speed_kph", "throttle_pct", "brake_applied", "brake_pct",
                      "steer_pct", "gear", "rpm", "drs")
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
    def __init__(self, data_dir: str | Path) -> None:
        self.repo = HistoricalRepository(Path(data_dir) / "historical")
        self._client: FastF1Client | None = None
        self._jobs: dict[str, dict] = {}
        self._driver_cache: dict[tuple, list[dict]] = {}

    @property
    def client(self) -> FastF1Client:
        if self._client is None:
            self._client = FastF1Client(self.repo.cache_dir)
        return self._client

    # ------------------------------------------------------------- schedule browsing
    def events(self, year: int) -> list[dict]:
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

    def sessions_for_event(self, year: int, event: str) -> list[dict]:
        schedule = self.client.get_schedule(year)
        name = resolve_event(schedule, event)
        row = schedule[schedule["EventName"] == name].iloc[0]
        return _schedule_sessions(row)

    async def drivers_preview(self, year: int, event: str, session: str) -> list[dict]:
        """Driver list for a schedule session, without a full telemetry import (laps only)."""
        code = _session_code(session)
        key = (year, event.strip().lower(), code)
        if key in self._driver_cache:
            return self._driver_cache[key]
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
    async def start_import(self, req: HistoricalImportRequest) -> dict:
        job = {"import_id": "imp-" + uuid.uuid4().hex[:10], "state": "QUEUED", "detail": "queued",
               "error": None, "created_at": datetime.now(UTC).isoformat(), "finished_at": None,
               "request": req.model_dump(), "session_id": None}
        self._jobs[job["import_id"]] = job
        asyncio.create_task(self._run_import(job, req.model_dump()))
        return {"import_id": job["import_id"], "state": job["state"]}

    async def _run_import(self, job: dict, payload: dict) -> None:
        def progress(state: str, detail: str) -> None:
            job["state"], job["detail"] = state, detail

        try:
            manifest = await asyncio.to_thread(import_session, self.client, self.repo, payload, progress)
            job.update(state="COMPLETED", detail="import complete", session_id=manifest["session_id"],
                       finished_at=datetime.now(UTC).isoformat())
        except FastF1Unavailable as exc:
            job.update(state="FAILED", error=str(exc), finished_at=datetime.now(UTC).isoformat())
        except Exception as exc:  # network errors, missing data, bad driver codes
            job.update(state="FAILED", error=str(exc), finished_at=datetime.now(UTC).isoformat())

    def import_status(self, import_id: str) -> dict:
        job = self._jobs.get(import_id)
        if job is None:
            from f1lab.services.lab import NotFound
            raise NotFound(import_id)
        return {k: v for k, v in job.items() if k != "request"} | {"request": job["request"]}

    # ------------------------------------------------------------- stored sessions
    def _manifest(self, sid: str) -> dict:
        m = self.repo.manifest(sid)
        if m is None:
            from f1lab.services.lab import NotFound
            raise NotFound(sid)
        return m

    def list_sessions(self) -> list[dict]:
        return self.repo.list_sessions()

    def get_session(self, sid: str) -> dict:
        return self._manifest(sid)

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
        m = self._manifest(sid)
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
