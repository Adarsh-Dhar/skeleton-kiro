"""HistoricalService: coordinates OpenF1 imports (background jobs) and queries on stored sessions.

Historical sessions and import jobs live in the root database (``f1_hist_*``, ``f1_import_job``).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import Engine

from f1lab.errors import Conflict, NotFound
from f1lab.integrations.historical import analysis, csv_importer, csv_normalizer, openf1_importer, openf1_normalizer
from f1lab.integrations.historical.importer import (
    CANCELLED,
    DUPLICATE_KEY_CONFLICT,
    NETWORK_TIMEOUT,
    HistoricalImportError,
    ImportCancelled,
    _session_code,
    classify,
)
from f1lab.integrations.historical.jobs import ACTIVE, ImportJobStore, dedupe_key
from f1lab.integrations.historical.openf1_client import OpenF1Client
from f1lab.integrations.historical.openf1_client import cache_stats as openf1_cache_stats
from f1lab.integrations.historical.providers import PROVIDERS, describe_session
from f1lab.integrations.historical.repository import HistoricalRepository
from f1lab.integrations.historical.schemas import HistoricalCompareRequest, HistoricalImportRequest, SeasonCompareRequest

TELEMETRY_CHANNELS = ("distance_m", "session_time_s", "speed_kph", "throttle_pct", "brake_applied", "brake_pct",
                      "steer_pct", "gear", "rpm", "drs")
DATASETS = ("race_position", "session_result", "starting_grid", "intervals", "overtakes", "team_radio",
            "drivers_championship", "teams_championship")
MANIFEST_DATASETS = ("pit_stops", "race_control")  # small lists kept in the session manifest
EXPORT_TABLES = ("laps", "results", "standings", "weather", "tyre_degradation", "mini_sectors")
EXPORT_HEADERS = {  # used only when a table is empty, so the CSV still has a header row
    "laps": ("driver", "driver_number", "team", "lap_number", "stint", "lap_time_s", "sector_1_s", "sector_2_s",
             "sector_3_s", "compound"),
    "results": ("position", "driver", "laps", "points", "gap_to_leader_text", "dnf", "dns", "dsq"),
    "standings": ("kind", "position", "name", "team", "points_start", "points_current"),
    "weather": ("session_time_s", "air_temp_c", "track_temp_c", "humidity_pct", "pressure_hpa", "wind_speed_ms",
                "wind_direction_deg", "rainfall_flag"),
    "tyre_degradation": ("driver", "stint", "compound", "tyre_age_first", "tyre_age_last", "laps_used",
                         "slope_s_per_lap", "r_squared", "reason"),
    "mini_sectors": ("index", "start_frac", "end_frac", "time_s", "ref_time_s", "delta_s", "cum_delta_s"),
}
UPLOAD_CHUNK_BYTES = 1 << 20  # staging reads the upload in 1 MiB pieces
CSV_VALIDATE_ROW_CAP = 100_000  # preview/validate check at most this many rows of a sampled file
REPLACE_DECISION_TIMEOUT_S = 300.0  # a paused duplicate-key import is discarded after 5 minutes without a choice (Req 10.4)
REPLACE_OPTIONS = ("replace", "cancel")
POSITION_CHANNELS = ("distance_m", "session_time_s", "x_m", "y_m", "z_m", "speed_kph")


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


class StorageError(RuntimeError):
    """A storage operation (session deletion) failed and was rolled back; the message is shown to the user (HTTP 500)."""


class HistoricalService:
    """``engine`` is the root database; ``cache_dir`` holds the cached OpenF1 answers (reused by every import).

    Setup_Link hook (Task 6.3): ``experiment_link_lookup`` is an optional callable ``(session_id) -> list[dict]`` returning
    one entry per TRIALFORGE experiment that holds measurements imported from that session. Each entry needs
    ``experiment_id`` and may carry ``name``. It is read by ``delete_session`` only, to warn and require confirmation. Task 6.3
    installs it with ``set_experiment_link_lookup``; until then no session is considered linked. Deleting a session never
    touches experiments: measurements and their provenance live in the Setup tables, with no foreign key to ``f1_hist_*``."""

    def __init__(self, engine: Engine, cache_dir: str | Path, openf1_client_factory: Callable[[], Any] | None = None,
                 job_timeout_s: float = 1800.0) -> None:
        self.repo = HistoricalRepository(engine)
        self.openf1_cache_dir = Path(cache_dir)
        self.upload_dir = self.openf1_cache_dir.parent / "csv-uploads"  # staged CSV uploads, content-addressed
        self.jobs = ImportJobStore(engine)
        self._openf1: OpenF1Client | None = None
        self._openf1_factory = openf1_client_factory
        self._driver_cache: dict[tuple, list[dict]] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._gate = asyncio.Semaphore(1)  # one import at a time: they share OpenF1's request budget
        self.job_timeout_s = job_timeout_s
        self.replace_timeout_s = REPLACE_DECISION_TIMEOUT_S
        self._pending: dict[str, dict] = {}  # import_id -> paused duplicate-key decision (in memory: a restart fails such jobs)
        self.experiment_link_lookup: Callable[[str], list[dict]] | None = None

    def set_experiment_link_lookup(self, lookup: Callable[[str], list[dict]] | None) -> None:
        """Install the Setup_Link lookup described in the class docstring (None removes it)."""
        self.experiment_link_lookup = lookup

    def recover(self) -> int:
        """Startup recovery: QUEUED, PROCESSING and SAVING jobs become FAILED with reason ``interrupted`` and any session
        left in a non-complete status is removed, so no partial session is ever exposed (Req 8.6, 8.7). Returns the job count."""
        n = self.jobs.mark_interrupted()
        self.repo.purge_incomplete()
        return n

    async def shutdown(self) -> None:
        for jid, task in list(self._tasks.items()):
            if not task.done():
                self.jobs.request_cancel(jid)
                task.cancel()
        self._tasks.clear()

    @property
    def openf1(self) -> OpenF1Client:
        if self._openf1 is None:
            self._openf1 = self._openf1_factory() if self._openf1_factory else OpenF1Client(self.openf1_cache_dir)
        return self._openf1

    # ------------------------------------------------------------- schedule browsing
    def events(self, year: int) -> list[dict]:
        openf1_importer.check_year(year)
        meetings = self.openf1.get("meetings", {"year": year})
        if not openf1_normalizer.schedule_frame(meetings).shape[0]:
            return []  # a season OpenF1 has not published yet is an empty list, not an error
        return openf1_normalizer.event_list(meetings, self.openf1.get("sessions", {"year": year}))

    def sessions_for_event(self, year: int, event: str) -> list[dict]:
        meeting = openf1_importer.resolve_meeting(openf1_importer.meetings_for(self.openf1, year), event)
        return openf1_normalizer.schedule_sessions(self.openf1.get("sessions", {"meeting_key": meeting["meeting_key"]}))

    async def drivers_preview(self, year: int, event: str, session: str) -> list[dict]:
        """Driver list of a session, without importing anything."""
        code = _session_code(session)
        key = (year, event.strip().lower(), code)
        if key not in self._driver_cache:
            self._driver_cache[key] = await asyncio.to_thread(openf1_importer.preview, self.openf1, year, event, session)
        return self._driver_cache[key]

    # ------------------------------------------------------------- imports
    async def start_import(self, req: HistoricalImportRequest, force: bool = False) -> dict:
        """Queue an import. A request identical to one already running, or one already completed whose session is
        still stored, returns that job (``duplicate: true``) instead of downloading again, unless ``force``."""
        payload = req.model_dump()
        force = force or req.refresh_cache  # a cache refresh always runs again
        key = dedupe_key(payload)
        active = self.jobs.find_active(key)
        if active is not None:
            return {**active, "duplicate": True}
        if not force and payload.get("provider") != "csv_upload":  # a repeated CSV import is judged by the importer (Req 10.1-10.3)
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
        """Run a job. A CSV import whose Session_Metadata key matches a stored session pauses OUTSIDE the import gate until the
        user chooses ``replace`` or ``cancel``; no choice within ``replace_timeout_s`` discards the import (Req 10.3-10.6)."""
        conflict = await self._run_import_once(import_id, payload, force, replacing=False)
        if conflict is None:
            return
        jobs = self.jobs
        existing = conflict.details.get("session_id")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        expires = datetime.now(UTC) + timedelta(seconds=self.replace_timeout_s)
        self._pending[import_id] = {"future": fut, "loop": loop, "existing_session_id": existing, "expires_at": expires,
                                    "options": list(REPLACE_OPTIONS)}
        jobs.progress(import_id, "PROCESSING", f"paused: session {existing} has the same provider, event, session type, driver and year; "
                                               "replace it or cancel (no answer within 5 minutes cancels this import)")
        try:
            choice = await asyncio.wait_for(fut, timeout=self.replace_timeout_s)
        except TimeoutError:
            choice = "expired"
        except asyncio.CancelledError:
            jobs.update(import_id, state="CANCELLED", error_code=CANCELLED, error="the application shut down", detail="cancelled")
            raise
        finally:
            self._pending.pop(import_id, None)
        if choice != "replace":
            why = ("replacement was not confirmed within 5 minutes" if choice == "expired" else "cancelled by the user")
            jobs.update(import_id, state="CANCELLED", error_code=CANCELLED, error=f"{why}; the existing session was left unchanged",
                        detail="cancelled")
            return
        replaced = {**payload, "csv": {**(payload.get("csv") or {}), "replace_existing": True}}
        jobs.progress(import_id, "PROCESSING", f"replacing session {existing}")
        await self._run_import_once(import_id, replaced, force, replacing=True)

    async def _run_import_once(self, import_id: str, payload: dict, force: bool, replacing: bool) -> HistoricalImportError | None:
        """One attempt. Returns the duplicate-key conflict when the job must pause for a decision, otherwise None."""
        jobs = self.jobs
        if self._gate.locked():
            jobs.progress(import_id, "QUEUED", "waiting for another import to finish")
        async with self._gate:
            if jobs.cancel_requested(import_id):
                jobs.update(import_id, state="CANCELLED", error_code=CANCELLED, error="cancelled before it started", detail="cancelled")
                return None
            timed_out = False

            def progress(state: str, detail: str) -> None:
                jobs.progress(import_id, state if state in ACTIVE else "PROCESSING", detail)

            def should_cancel() -> bool:
                return timed_out or jobs.cancel_requested(import_id)

            before = openf1_cache_stats(self.openf1_cache_dir)
            work = asyncio.to_thread(PROVIDERS[payload.get("provider") or "openf1"].run, self.repo, payload, progress, should_cancel,
                                    client=self.openf1, force=force, upload_dir=self.upload_dir)
            try:
                manifest = await asyncio.wait_for(asyncio.shield(work), timeout=self.job_timeout_s)
            except TimeoutError:
                timed_out = True  # the worker thread stops at its next checkpoint and stores nothing
                jobs.update(import_id, state="FAILED", error_code=NETWORK_TIMEOUT,
                            error=f"the import did not finish within {int(self.job_timeout_s)} s; nothing was stored. Retry it.",
                            detail="timed out")
                return None
            except asyncio.CancelledError:
                jobs.update(import_id, state="CANCELLED", error_code=CANCELLED, error="the application shut down", detail="cancelled")
                raise
            except ImportCancelled:
                jobs.update(import_id, state="CANCELLED", error_code=CANCELLED, error="cancelled by the user; nothing was stored", detail="cancelled")
                return None
            except Exception as exc:  # network errors, missing data, bad driver codes
                if (not replacing and isinstance(exc, HistoricalImportError) and exc.code == DUPLICATE_KEY_CONFLICT
                        and payload.get("provider") == "csv_upload"):
                    return exc
                msg = str(exc) or type(exc).__name__
                if replacing:  # the one-transaction replacement rolled back: the stored session is intact (Req 10.6)
                    msg = f"replacement failed; the existing session was kept unchanged: {msg}"
                jobs.update(import_id, state="FAILED", error_code=classify(exc), error=msg[:500], detail="failed")
                return None
            after = openf1_cache_stats(self.openf1_cache_dir)
            if manifest.get("reused"):
                note = "already stored; reused without downloading"
            elif after["bytes"] > before["bytes"]:
                note = f"import complete; downloaded {round((after['bytes'] - before['bytes']) / 1e6, 1)} MB into the OpenF1 cache"
            else:
                note = "import complete; served from the OpenF1 cache (no new downloads)"
            if manifest.get("warnings"):
                note += "; warnings: " + "; ".join(manifest["warnings"][:5])
            jobs.update(import_id, state="COMPLETED", detail=note, session_id=manifest["session_id"], error=None, error_code=None)
            return None

    def _with_decision(self, job: dict) -> dict:
        """Add ``awaiting_decision`` (existing session, options, expiry) to a job paused on a duplicate-key conflict."""
        p = self._pending.get(job["import_id"])
        if p is None or job["state"] not in ACTIVE:
            return job
        return {**job, "awaiting_decision": {"existing_session_id": p["existing_session_id"], "options": p["options"],
                                             "expires_at": p["expires_at"].isoformat()}}

    def import_status(self, import_id: str) -> dict:
        job = self.jobs.get(import_id)
        if job is None:
            raise NotFound(import_id)
        return self._with_decision(job)

    def list_imports(self, limit: int = 50) -> list[dict]:
        return [self._with_decision(j) for j in self.jobs.list(limit)]

    def _resolve_pending(self, import_id: str, choice: str) -> bool:
        p = self._pending.get(import_id)
        if p is None:
            return False
        p["loop"].call_soon_threadsafe(lambda: p["future"].done() or p["future"].set_result(choice))
        return True

    def decide_duplicate(self, import_id: str, choice: str) -> dict:
        """Answer a paused duplicate-key import: ``replace`` the existing session or ``cancel`` (Req 10.3-10.5).
        404 for an unknown job; 409 when the job is not waiting for a choice or the 5 minutes have passed."""
        self.import_status(import_id)
        if choice not in REPLACE_OPTIONS:
            raise ValueError(f"choice must be one of {', '.join(REPLACE_OPTIONS)}")
        p = self._pending.get(import_id)
        if p is None or p["expires_at"] <= datetime.now(UTC):
            raise Conflict(f"import {import_id} is not waiting for a replace-or-cancel choice (it may have expired)")
        if choice == "cancel":
            return self.cancel_import(import_id)
        self._resolve_pending(import_id, "replace")
        return self.import_status(import_id)

    def cancel_import(self, import_id: str) -> dict:
        job = self.import_status(import_id)
        if job["state"] not in ACTIVE:
            raise Conflict(f"import {import_id} is {job['state']}; only queued or running imports can be cancelled")
        self.jobs.request_cancel(import_id)
        if import_id in self._pending:  # paused on a duplicate: finish it now, leave the existing session unchanged
            self.jobs.update(import_id, state="CANCELLED", error_code=CANCELLED,
                             error="cancelled by the user; the existing session was left unchanged", detail="cancelled")
            self._resolve_pending(import_id, "cancel")
        elif import_id not in self._tasks:  # not running in this process (should not happen after recover())
            self.jobs.update(import_id, state="CANCELLED", error_code=CANCELLED, error="cancelled", detail="cancelled")
        else:
            self.jobs.progress(import_id, job["state"], "cancelling: stops at the next step (an in-flight download finishes first)")
        return self.import_status(import_id)

    async def retry_import(self, import_id: str) -> dict:
        job = self.import_status(import_id)
        if job["state"] not in ("FAILED", "CANCELLED"):
            raise Conflict(f"import {import_id} is {job['state']}; only failed or cancelled imports can be retried")
        if (job["request"].get("provider") or "openf1") not in PROVIDERS:
            raise Conflict(f"import {import_id} used a data provider this app no longer supports; start a new import instead")
        if (job["request"].get("provider") or "openf1") == "csv_upload":
            h = str((job["request"].get("csv") or {}).get("file_hash") or "").lower()
            if not (self.upload_dir / f"{h}.csv").is_file():  # reject the request itself; no new job is created (Req 9.10)
                raise Conflict(f"import {import_id} cannot be retried because its uploaded file is no longer stored; "
                               "upload the file again and start a new import")
        active = self.jobs.find_active(dedupe_key(job["request"]))
        if active is not None:
            return {**active, "duplicate": True}
        new = self.jobs.create(job["request"], retry_of=import_id, attempts=job["attempts"] + 1)
        self._spawn(new["import_id"], job["request"], False)
        return {**new, "duplicate": False}

    def _importing_jobs(self, sid: str, manifest: dict) -> list[dict]:
        """PROCESSING or SAVING jobs that are writing (or replacing) this session. A CSV job matches by file hash or
        Session_Metadata key; an OpenF1 job by year, event and session type (the resolved event name may differ from the
        request, so matching is by containment and errs towards blocking)."""
        meta = manifest.get("metadata") or {}
        hits = []
        for j in self.jobs.running():
            req = j["request"]
            if j.get("session_id") == sid:
                hits.append(j)
            elif (req.get("provider") or "openf1") == "csv_upload":
                c = req.get("csv") or {}
                m = c.get("metadata") or {}
                same_file = bool(manifest.get("file_hash")) and str(c.get("file_hash") or "").lower() == manifest["file_hash"]
                same_key = all(m.get(k) is not None and m.get(k) == meta.get(k) for k in ("event", "session_type", "driver", "year"))
                if same_file or same_key:
                    hits.append(j)
            else:
                ev = str(req.get("event") or "").strip().lower()
                stored = f"{manifest.get('event') or ''} {manifest.get('circuit') or ''}".lower()
                code = str(req.get("session") or "").strip().upper()
                kinds = {str(manifest.get(k) or "").upper() for k in ("session", "session_type")}
                if req.get("year") == manifest.get("year") and ev and ev in stored and (not code or code in kinds or not kinds - {""}):
                    hits.append(j)
        return hits

    def delete_session(self, sid: str, confirm_linked: bool = False) -> dict:
        """Delete a session and all its ``f1_hist_*`` rows in one transaction.

        404 for an unknown id. 409 while an import for it is PROCESSING or SAVING (Req 10.10), and 409 naming the linked
        TRIALFORGE experiments until ``confirm_linked`` is true (Req 10.9; the experiments' measurements are never touched).
        A storage failure rolls back, keeps the session and raises ``StorageError`` (Req 10.8)."""
        manifest = self.repo.manifest(sid)
        if manifest is None:
            raise NotFound(sid)
        if self._importing_jobs(sid, manifest):
            raise Conflict(f"session {sid} cannot be deleted: an import is in progress for it; wait for it to finish or cancel it")
        links = list(self.experiment_link_lookup(sid) or []) if self.experiment_link_lookup else []
        if links and not confirm_linked:
            names = ", ".join(f"{(x.get('name') or x.get('experiment_id'))} ({x.get('experiment_id')})" for x in links)
            raise Conflict(f"session {sid} is linked to TRIALFORGE experiment(s) {names}. Deleting it keeps those experiments' "
                           "measurements and their provenance. Repeat the request with confirm_linked=true to delete the session")
        try:
            deleted = self.repo.delete_session(sid)
        except Exception as exc:
            raise StorageError(f"deleting session {sid} failed and was rolled back; the session is unchanged ({type(exc).__name__})") from exc
        if not deleted:
            raise NotFound(sid)
        return {"deleted": sid, "linked_experiments": [x.get("experiment_id") for x in links]}

    # ------------------------------------------------------------- CSV upload workflow (preview, validate, session-exists)
    async def stage_csv_upload(self, source: Any, filename: str | None) -> dict:
        """Stream ``source`` (an object with ``async read(n)``) into ``{upload_dir}/{sha256}.csv`` without holding it in memory.

        The file is content-addressed, so uploading the same bytes again reuses the staged file. An upload larger than the
        configured maximum raises ``CsvLimitError(FILE_TOO_LARGE)`` and nothing is kept (Req 11.13, 12.4)."""
        limit = csv_importer.CSV_MAX_UPLOAD_BYTES
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.upload_dir / f".part-{uuid.uuid4().hex}"
        digest, size = hashlib.sha256(), 0
        try:
            with open(tmp, "wb") as fh:
                while chunk := await source.read(UPLOAD_CHUNK_BYTES):
                    size += len(chunk)
                    if size > limit:
                        raise csv_importer.CsvLimitError(csv_importer.CsvLimitError.FILE_TOO_LARGE, "maximum file size (bytes)", limit)
                    digest.update(chunk)
                    fh.write(chunk)
            if size == 0:
                raise ValueError("the uploaded file is empty")
            file_hash = digest.hexdigest()
            final = self.upload_dir / f"{file_hash}.csv"
            created = not final.exists()
            os.replace(tmp, final)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        name = Path((filename or "").replace("\\", "/")).name or "upload.csv"
        return {"file_hash": file_hash, "path": final, "created": created, "size_bytes": size, "original_filename": name[:255]}

    def csv_file(self, file_hash: str) -> Path:
        """Path of a staged upload; NotFound (HTTP 404) when it was never uploaded or has been removed."""
        path = self.upload_dir / f"{file_hash.lower()}.csv"
        if not path.is_file():
            raise NotFound(f"uploaded file {file_hash}; upload it again with the preview endpoint")
        return path

    async def csv_preview(self, source: Any, filename: str | None) -> dict:
        """Stage an upload and inspect it. Creates no session and no import job (Req 11.1)."""
        staged = await self.stage_csv_upload(source, filename)
        try:
            out = await asyncio.to_thread(self._csv_preview, staged["path"])
        except BaseException:
            if staged["created"]:
                staged["path"].unlink(missing_ok=True)  # an unreadable new file is not kept
            raise
        rec = self.repo.record_upload(staged["file_hash"], staged["original_filename"], staged["size_bytes"])
        return {"file_hash": rec["file_hash"], "original_filename": rec["original_filename"], "size_bytes": rec["size_bytes"], **out}

    @staticmethod
    def _csv_preview(path: Path) -> dict:
        ins = csv_importer.inspect(path)
        if not ins.columns:
            raise ValueError("the file has no header row")
        if ins.row_count == 0:
            raise ValueError("the file has no data rows")
        kind = csv_importer.classify(ins.columns)
        suggested = csv_importer.suggest_mapping(ins.columns)
        report = HistoricalService._csv_report(path, ins.columns, kind, suggested, {}, "reject", "reject", None, None, None)
        return {"columns": ins.columns, "column_count": len(ins.columns), "row_count": ins.row_count, "sample_rows": ins.sample_rows,
                "suggested_mapping": suggested, "detected_type": kind,
                "data_type_choices": {"lap_summary": ["lap_summary"], "sampled_telemetry": ["sampled_telemetry"],
                                      "ambiguous": ["lap_summary", "sampled_telemetry"]}.get(kind, []),
                "validation_report": report}

    @staticmethod
    def _csv_report(path: Path, columns: list[str], kind: str | None, mapping: dict, units: dict, dup: str, order: str,
                    driver: str | None, tz_offset: str | None, timezone: str | None) -> dict:
        """Row validation of the file (the first ``CSV_VALIDATE_ROW_CAP`` rows of a sampled file) as a plain dict."""
        base = {"ok": False, "data_type": kind, "total_rows": 0, "errors": [], "error_count": 0, "warnings": [], "warning_count": 0}
        if kind == csv_importer.LAP_SUMMARY:
            v = csv_importer.validate_lap_summary(path)
            errs = []
            if v.missing_columns:
                errs.append({"row": 0, "column": ", ".join(v.missing_columns), "value": None, "reason": "missing_required_column", "severity": "error"})
            if v.first_error:
                e = v.first_error
                errs.append({"row": e.row, "column": e.column, "value": e.value, "reason": e.reason, "severity": "error"})
            return {**base, "ok": v.ok, "total_rows": v.row_count, "errors": errs, "error_count": len(errs), "missing_columns": v.missing_columns}
        if kind != csv_importer.SAMPLED_TELEMETRY:
            reason = "choose_data_type" if kind == csv_importer.AMBIGUOUS else "unrecognized_columns"
            return {**base, "errors": [{"row": 0, "column": "", "value": None, "reason": reason, "severity": "error"}], "error_count": 1}
        cols = list(dict.fromkeys(mapping.values()))
        if not ({"session_time_s", "timestamp"} & set(mapping)):
            err = {"row": 0, "column": "", "value": None, "reason": "no_time_column_mapped", "severity": "error"}
            return {**base, "errors": [err], "error_count": 1}
        driver_col = mapping.get("driver")
        pieces: list[pd.DataFrame] = []
        rows = 0
        truncated = False
        try:
            for chunk in csv_importer.read_chunks(path, csv_importer.CHUNK_ROWS):
                if driver_col and driver:
                    chunk = chunk[chunk[driver_col] == driver]
                pieces.append(chunk[cols])
                rows += len(chunk)
                if rows >= CSV_VALIDATE_ROW_CAP:
                    truncated = True
                    break
        except csv_importer.CsvLimitError as exc:
            err = {"row": 0, "column": "", "value": exc.limit, "reason": f"{exc.code}: {exc}", "severity": "error"}
            return {**base, "errors": [err], "error_count": 1}
        frame = pd.concat(pieces, ignore_index=True) if pieces else pd.DataFrame(columns=cols)
        frame = frame.iloc[:CSV_VALIDATE_ROW_CAP]
        policy = {"duplicate_policy": dup, "order_policy": order, "data_type": csv_importer.SAMPLED_TELEMETRY,
                  "tz_offset": tz_offset, "timezone": timezone}
        groups = {driver or "": frame}
        if driver_col and not driver:
            keys = frame[driver_col].map(lambda v: v.strip() or csv_importer.UNKNOWN_DRIVER)
            groups = {str(k): frame[keys == k].reset_index(drop=True) for k in dict.fromkeys(keys)}
        reports = {k: csv_importer.validate_rows(g, mapping, units, policy).to_dict()
                   for k, g in groups.items()}
        if len(reports) == 1:
            out = dict(next(iter(reports.values())))
            out["per_driver"] = False
        else:
            out = {"per_driver": True, "ok": all(r["ok"] for r in reports.values()), "coverage": {k: r["coverage"] for k, r in reports.items()}}
            for key in ("total_rows", "samples", "duplicates_dropped", "duplicate_group_count", "duplicate_rows_affected", "out_of_order_count",
                        "rows_moved", "skipped_time_rows", "error_count", "warning_count"):
                out[key] = sum(r.get(key, 0) for r in reports.values())
            for key in ("errors", "warnings"):
                out[key] = [{**e, "driver": k} for k, r in reports.items() for e in r[key]][:csv_importer.MAX_REPORTED_ERRORS]
        return {**base, **out, "data_type": kind, "truncated": truncated, "rows_checked": len(frame)}

    def csv_validate(self, req: Any) -> dict:
        """Validate a staged file against a mapping, units, policies and Session_Metadata. Imports nothing (Req 6.11, 11.1)."""
        path = self.csv_file(req.file_hash)
        columns = [c.strip() for c in csv_importer.read_header(path)]
        detected = csv_importer.classify(columns)
        kind = req.data_type or detected
        requested = req.mapping.root if req.mapping is not None else csv_importer.suggest_mapping(columns)
        mres = csv_importer.validate_mapping(requested, columns)
        upload = self.repo.upload(req.file_hash)
        meta_in = req.metadata.model_dump()
        meta_out: dict[str, Any]
        try:
            units, supplied = csv_importer._resolve_units({"units": req.units.model_dump()}, meta_in)
            meta_out = {"ok": True, "values": csv_importer.validate_session_metadata(meta_in, (upload or {}).get("original_filename"), supplied),
                        "missing": [], "invalid": []}
        except HistoricalImportError as exc:
            units = {}
            meta_out = {"ok": False, "values": None, "missing": exc.details.get("missing", []), "invalid": exc.details.get("invalid", []),
                        "message": str(exc)}
        driver = meta_in.get("driver")
        report = None
        if mres.ok and kind in (csv_importer.LAP_SUMMARY, csv_importer.SAMPLED_TELEMETRY):
            report = self._csv_report(path, columns, kind, csv_normalizer._canonical_mapping(mres.mapping), units, req.duplicate_policy,
                                      req.order_policy, driver.strip() if isinstance(driver, str) and driver.strip() else None,
                                      req.tz_offset, meta_in.get("timezone"))
        elif mres.ok:
            report = self._csv_report(path, columns, kind, {}, units, req.duplicate_policy, req.order_policy, None, None, None)
        existing = self.repo.find_by_file_hash(req.file_hash)
        return {"file_hash": req.file_hash, "original_filename": (upload or {}).get("original_filename"), "detected_type": detected,
                "data_type": kind, "ok": bool(mres.ok and meta_out["ok"] and report and report["ok"] and not existing),
                "mapping": {"ok": mres.ok, "errors": mres.errors, "accepted": mres.mapping if mres.ok else {}},
                "metadata": meta_out, "report": report, "existing_session_id": existing}

    def csv_session_exists(self, q: Any) -> dict:
        """Does a stored session match a file hash or a Session_Metadata key? (Req 10.1, 10.2, 11.8)"""
        if q.file_hash:
            sid = self.repo.find_by_file_hash(q.file_hash.lower())
            if sid:
                return {"exists": True, "session_id": sid, "matched_by": "file_hash"}
        if q.event is not None:
            sid = self.repo.find_by_key({"provider": csv_normalizer.PROVIDER_LABEL, "event": q.event, "session_type": q.session_type,
                                         "driver": q.driver, "year": q.year})
            if sid:
                return {"exists": True, "session_id": sid, "matched_by": "session_key"}
        return {"exists": False, "session_id": None, "matched_by": None}

    # ------------------------------------------------------------- stored sessions
    def _manifest(self, sid: str) -> dict:
        m = self.repo.manifest(sid)
        if m is None:
            raise NotFound(sid)
        return m

    def list_sessions(self) -> list[dict]:
        keys = ("session_id", "year", "event", "circuit", "session", "session_type", "session_date", "imported_at", "drivers", "channels")
        return [{**{k: m.get(k) for k in keys}, **describe_session(m)} for m in self.repo.list_manifests()]

    def get_session(self, sid: str) -> dict:
        m = self._manifest(sid)
        return {**m, **describe_session(m)}

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
        return [{**d, "team_colour": d.get("team_colour"), "headshot_url": d.get("headshot_url")}
                for d in self._manifest(sid).get("drivers", [])]

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
        df, masked = analysis.mask_weather_dropouts(self.repo.weather(sid))
        out = {"samples": _records(df), "count": len(df), "note": "weather is sampled sparsely by the timing feed"}
        if masked:
            what = ", ".join(f"{n} {c}" for c, n in masked.items())
            out["masked_dropouts"] = masked
            out["note"] += (f". Treated as missing, not as real readings: {what} sample(s) of exactly 0 that OpenF1 reported "
                            "while the other channels were normal (sensor dropout). The stored data is unchanged.")
        return out

    def track_map(self, sid: str, driver: str | None = None, lap: int | None = None) -> dict:
        m = self._manifest(sid)
        out: dict[str, Any] = {"corners": m.get("corners", []), "circuit": m.get("circuit")}
        if driver:
            code = self._driver_code(sid, driver)
            pos = self.position(sid, code, lap)
            out.update(driver=code, lap=lap, points=pos["points"][:5000], count=pos["count"])
        return out

    # ------------------------------------------------------------- analysis (read-only)
    def tyre_degradation(self, sid: str) -> dict:
        m = self._manifest(sid)  # NotFound for an unknown session
        laps = self.repo.laps(sid)
        has_stints = (not laps.empty and "stint" in laps.columns and "driver" in laps.columns
                      and bool(laps["stint"].notna().any()))
        if not has_stints:
            return {"stints": [], "by_compound": [], "caveat": analysis.FUEL_CAVEAT,
                    "coverage_note": "no stint data is stored for this session; tyre degradation cannot be computed"}
        # laps may carry the driver number; report the three-letter code
        code_of = {str(d.get("number", "")): str(d.get("code", "")) for d in m.get("drivers", []) if d.get("number") not in (None, "")}
        laps = laps.copy()
        laps["driver"] = laps["driver"].astype(str).map(lambda v: code_of.get(v, v))
        if "is_valid" not in laps.columns or not laps["is_valid"].notna().any():
            laps["is_valid"] = True  # OpenF1 may not publish validity: missing means valid, not excluded
        return analysis.tyre_degradation(laps, m.get("race_control") or [])

    def compare_sessions(self, req: SeasonCompareRequest) -> dict:
        """Compare drivers across stored sessions (read-only; Requirements 4.1 to 4.7)."""
        manifests = [(sid, self._manifest(sid)) for sid in req.sessions]  # NotFound names the first unknown id
        drivers = [d.strip().upper() for d in req.drivers]
        sessions = []
        for sid, m in manifests:
            datasets = {}
            if req.metric in ("finish_position", "grid_position", "points"):
                name = analysis._DATASET_FOR_METRIC[req.metric]
                datasets[name] = self.repo.dataset(sid, name) or None
            laps = self.repo.laps(sid) if req.metric in ("best_lap", "median_lap") else None
            if laps is not None and not laps.empty:
                laps = laps.copy()
                code_of = {str(d.get("number", "")): str(d.get("code", "")) for d in m.get("drivers", []) if d.get("number") not in (None, "")}
                laps["driver"] = laps["driver"].astype(str).map(lambda v, c=code_of: c.get(v, v))
            sessions.append({
                "id": sid, "year": m.get("year"), "event": m.get("event"), "session_type": m.get("session_type"),
                "date_start": m.get("session_date"), "drivers": m.get("drivers", []),
                "laps": laps, "datasets": datasets,
            })
        return analysis.season_compare(sessions, drivers, req.metric)

    def _lap_telemetry(self, sid: str, code: str, lap: int | None, laps: pd.DataFrame, driver_keys: set[str]) -> tuple[pd.DataFrame, int, str]:
        """Telemetry of one lap of a driver, the lap number used and how it was chosen ("requested" or "fastest_valid")."""
        tel = self.repo.telemetry(sid, code)
        if tel.empty:
            raise Conflict(f"no telemetry is stored for driver {code}; re-import with telemetry enabled")
        source = "requested"
        if lap is None:
            source = "fastest_valid"
            if laps.empty or "lap_time_s" not in laps.columns:
                raise Conflict(f"no lap times are stored for driver {code}, so the fastest valid lap cannot be chosen")
            mine = laps[laps["driver"].astype(str).isin(driver_keys)]
            ok = mine[mine["lap_time_s"].notna()]
            if "is_valid" in ok.columns and ok["is_valid"].notna().any():  # OpenF1 may not publish validity
                ok = ok[ok["is_valid"].fillna(False).astype(bool)]
            if "is_deleted" in ok.columns:
                ok = ok[~ok["is_deleted"].fillna(False).astype(bool)]
            have = set(tel["lap_number"].dropna().astype(int))
            ok = ok[ok["lap_number"].astype(int).isin(have)]
            if ok.empty:
                raise Conflict(f"driver {code} has no valid lap with telemetry")
            lap = int(ok.loc[ok["lap_time_s"].astype(float).idxmin(), "lap_number"])
        df = tel[tel["lap_number"] == lap]
        if df.empty:
            raise Conflict(f"no telemetry is stored for driver {code} lap {lap}")
        return df.sort_values("session_time_s"), lap, source
    def mini_sectors(self, sid: str, driver: str, lap: int | None = None, ref_driver: str | None = None,
                     ref_lap: int | None = None, count: int = 25) -> dict:
        m = self._manifest(sid)
        if isinstance(count, bool) or not isinstance(count, int) or not 10 <= count <= 50:
            raise ValueError("count must be an integer from 10 to 50")
        if ref_lap is not None and not ref_driver:
            ref_driver = driver  # a reference lap alone means another lap of the same driver
        entry = self._driver_entry(sid, driver)
        code = str(entry["code"])
        rentry = self._driver_entry(sid, ref_driver) if ref_driver else None  # unknown reference driver is a 422, before any 409
        laps = self.repo.laps(sid)
        keys = {str(entry.get("code", "")), str(entry.get("number", ""))} - {""}
        tel, used_lap, source = self._lap_telemetry(sid, code, lap, laps, keys)
        ref_tel = None
        ref_info = None
        if ref_driver:
            rcode = str(rentry["code"])
            rkeys = {str(rentry.get("code", "")), str(rentry.get("number", ""))} - {""}
            ref_tel, rused, rsource = self._lap_telemetry(sid, rcode, ref_lap, laps, rkeys)
            ref_info = {"driver": rcode, "lap": rused, "lap_source": rsource}
        out = analysis.mini_sectors(tel, count, ref_tel)
        out.update(driver=code, lap=used_lap, lap_source=source, session_id=m.get("session_id", sid))
        if ref_info:
            out["reference"] = ref_info
        return out
    # ------------------------------------------------------------- export (read-only)
    def standings(self, sid: str) -> list[dict]:
        """Both championship datasets as one flat list (kind driver/team), used by the CSV and the report."""
        m = self._manifest(sid)
        team_of = {str(d.get("code")): d.get("team") for d in m.get("drivers", [])}
        out: list[dict] = []
        for kind, ds in (("driver", "drivers_championship"), ("team", "teams_championship")):
            for r in self.repo.dataset(sid, ds):
                name = r.get("driver") if kind == "driver" else r.get("team")
                out.append({"kind": kind, "position": r.get("position_current"), "name": name,
                            "team": team_of.get(str(name)) if kind == "driver" else name,
                            "points_start": r.get("points_start"), "points_current": r.get("points_current")})
        return out

    def export_csv(self, sid: str, table: str, driver: str | None = None, lap: int | None = None,
                   ref_driver: str | None = None, ref_lap: int | None = None, count: int = 25) -> str:
        """CSV text for one table, built from the same data as the matching JSON route (Requirements 7.1, 7.3, 7.6)."""
        self._manifest(sid)  # 404 for an unknown session
        if table not in EXPORT_TABLES:
            raise ValueError(f"unknown table '{table}'; choose one of {', '.join(EXPORT_TABLES)}")
        if table == "laps":
            rows = self.laps(sid)
        elif table == "results":
            rows = self.repo.dataset(sid, "session_result")
        elif table == "standings":
            rows = self.standings(sid)
        elif table == "weather":
            rows = self.weather(sid)["samples"]
        elif table == "tyre_degradation":
            rows = []
            for s in self.tyre_degradation(sid)["stints"]:
                row = {k: v for k, v in s.items() if k != "laps_excluded"}
                for k, v in (s.get("laps_excluded") or {}).items():
                    row[f"laps_excluded_{k}"] = v
                rows.append(row)
        else:
            if not driver:
                raise ValueError("driver is required for the mini_sectors table")
            rows = self.mini_sectors(sid, driver, lap, ref_driver, ref_lap, count)["slices"]
        header: list[str] = []
        for r in rows:
            header += [k for k in r if k not in header]
        header = header or list(EXPORT_HEADERS[table])
        return analysis.csv_text(header, [[r.get(k) for k in header] for r in rows])

    def report_html(self, sid: str) -> str:
        """One self-contained HTML report for a stored session (Requirements 7.4, 7.7)."""
        m = self._manifest(sid)
        summary = {"event": m.get("event"), "session_type": m.get("session_type"), "date": m.get("session_date")}
        results = self.repo.dataset(sid, "session_result")
        standings = self.standings(sid)
        tyres = self.tyre_degradation(sid)
        weather_view = self.weather(sid)
        reasons = {"tyre_degradation": tyres.get("coverage_note"), "standings": self._standings_reason(m)}
        return analysis.report_html(summary, results, standings, tyres, weather_view["samples"], reasons,
                                    weather_note=weather_view["note"] if weather_view.get("masked_dropouts") else None)

    @staticmethod
    def _standings_reason(m: dict) -> str:
        """Why the championship standings are empty, from the import coverage (not an import failure when OpenF1 had none)."""
        endpoints = (m.get("coverage") or {}).get("endpoints") or {}
        found = [endpoints[k] for k in ("drivers_championship", "teams_championship") if k in endpoints]
        if not found:
            return "championship standings were not requested when this session was imported (re-import with race data to try)"
        if all(e.get("status") == "empty" for e in found):
            return ("OpenF1 published no championship standings for this session (a beta endpoint, often missing for recent races). "
                    "The import did not fail; re-import later to check again")
        problems = "; ".join(f"{e.get('endpoint')}: {e.get('status')}" + (f" ({e['note']})" if e.get("note") else "") for e in found)
        return f"championship standings could not be stored ({problems})"

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
