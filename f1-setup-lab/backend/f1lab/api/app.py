"""FastAPI application factory for F1 SETUP LAB (historical Formula 1 data from OpenF1)."""

from __future__ import annotations

import asyncio
import hashlib
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError as PydanticValidationError

from db import create_db_engine, migrate
from db.config import resolve_db_url, sqlite_file_path
from f1lab import HISTORICAL_NOTICE, __version__
from f1lab.errors import Conflict, NotFound
from f1lab.integrations.historical.openf1_client import MIN_YEAR, OpenF1Error
from f1lab.integrations.historical.schemas import HistoricalCompareRequest, HistoricalImportRequest
from f1lab.integrations.historical.service import HistoricalService

DEFAULT_FRONTEND = Path(__file__).resolve().parents[3] / "frontend"


def _issue(field: str, message: str) -> dict:
    return {"field": field, "message": message}


class _NoCacheStaticFiles(StaticFiles):
    """The app's own HTML/JS/CSS must never be served from a stale browser cache after an update."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


def create_app(db_path: str | None = None, frontend_dir: str | None = None, openf1_client_factory=None) -> FastAPI:
    """``db_path`` and ``openf1_client_factory`` are for tests; production uses the root database (TRIALFORGE_DB) and OpenF1."""
    url = resolve_db_url(db_path)
    engine = create_db_engine(url)
    migrate(engine)
    file = sqlite_file_path(url)
    data_dir = file.parent if file else Path("data")
    hist = HistoricalService(engine, data_dir / "openf1-cache", openf1_client_factory=openf1_client_factory)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.interrupted_imports = hist.recover()
        yield
        await hist.shutdown()

    app = FastAPI(title="F1 SETUP LAB", version=__version__, lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.historical = hist
    app.state.engine = engine

    # ------------------------------------------------------------ errors
    @app.exception_handler(RequestValidationError)
    async def _req(_: Request, exc: RequestValidationError):
        issues = [_issue(".".join(str(p) for p in e["loc"][1:]) or "request", e["msg"].removeprefix("Value error, ")) for e in exc.errors()]
        return JSONResponse({"detail": issues}, status_code=422)

    @app.exception_handler(PydanticValidationError)
    async def _pyd(_: Request, exc: PydanticValidationError):
        issues = [_issue(".".join(str(p) for p in e["loc"]) or "request", e["msg"].removeprefix("Value error, ")) for e in exc.errors()]
        return JSONResponse({"detail": issues}, status_code=422)

    @app.exception_handler(ValueError)
    async def _val(_: Request, exc: ValueError):
        return JSONResponse({"detail": [_issue("request", str(exc))]}, status_code=422)

    @app.exception_handler(OpenF1Error)
    async def _openf1(_: Request, exc: OpenF1Error):
        return JSONResponse({"detail": [_issue("openf1", str(exc))], "code": exc.code}, status_code=502)

    @app.exception_handler(NotFound)
    async def _nf(_: Request, exc: NotFound):
        return JSONResponse({"detail": [_issue("id", f"not found: {exc.args[0]}")]}, status_code=404)

    @app.exception_handler(Conflict)
    async def _conflict(_: Request, exc: Exception):
        return JSONResponse({"detail": [_issue("state", str(exc))]}, status_code=409)

    # ------------------------------------------------------------ meta
    @app.get("/api/health")
    def health():
        return {"status": "ok", "app": "f1-setup-lab", "version": __version__, "data": "openf1",
                "interrupted_imports": getattr(app.state, "interrupted_imports", 0)}

    @app.get("/api/meta")
    def meta():
        return {"version": __version__, "notice": HISTORICAL_NOTICE, "provider": "openf1", "first_year": MIN_YEAR}

    # ------------------------------------------------------------ historical F1 (OpenF1)
    year_q = Query(..., ge=MIN_YEAR, le=2100)

    @app.get("/api/historical/events")
    async def hist_events(year: int = year_q):
        return await asyncio.to_thread(hist.events, year)

    @app.get("/api/historical/event-sessions")
    async def hist_event_sessions(year: int = year_q, event: str = Query(..., min_length=1)):
        return await asyncio.to_thread(hist.sessions_for_event, year, event)

    @app.get("/api/historical/drivers")
    async def hist_drivers(year: int = year_q, event: str = Query(..., min_length=1), session: str = Query(..., min_length=1)):
        return await hist.drivers_preview(year, event, session)

    @app.get("/api/historical/openf1/cache")
    def hist_openf1_cache():
        return hist.openf1_cache()

    @app.delete("/api/historical/openf1/cache")
    def hist_openf1_cache_clear():
        """Forget every cached OpenF1 answer (stored sessions are not touched); the next import downloads again."""
        return hist.clear_openf1_cache()

    @app.post("/api/historical/import", status_code=202)
    async def hist_import(req: HistoricalImportRequest, force: bool = Query(False, description="re-download even if this session is already stored")):
        return await hist.start_import(req, force=force)

    @app.get("/api/historical/imports")
    def hist_imports(limit: int = Query(50, ge=1, le=500)):
        return hist.list_imports(limit)

    @app.get("/api/historical/imports/{import_id}")
    def hist_import_status(import_id: str):
        return hist.import_status(import_id)

    @app.post("/api/historical/imports/{import_id}/cancel")
    def hist_import_cancel(import_id: str):
        return hist.cancel_import(import_id)

    @app.post("/api/historical/imports/{import_id}/retry", status_code=202)
    async def hist_import_retry(import_id: str):
        return await hist.retry_import(import_id)

    @app.delete("/api/historical/sessions/{session_id}")
    def hist_delete_session(session_id: str):
        return hist.delete_session(session_id)

    @app.get("/api/historical/sessions")
    def hist_list_sessions():
        return hist.list_sessions()

    @app.get("/api/historical/sessions/{session_id}")
    def hist_session(session_id: str):
        return hist.get_session(session_id)

    @app.get("/api/historical/sessions/{session_id}/drivers")
    def hist_session_drivers(session_id: str):
        return hist.drivers(session_id)

    @app.get("/api/historical/sessions/{session_id}/laps")
    def hist_laps(session_id: str, driver: str | None = None):
        return hist.laps(session_id, driver)

    @app.get("/api/historical/sessions/{session_id}/telemetry")
    def hist_telemetry(session_id: str, driver: str = Query(..., min_length=1), lap: int | None = None, limit: int = Query(1500, ge=1, le=5000)):
        return hist.telemetry(session_id, driver, lap, limit)

    @app.get("/api/historical/sessions/{session_id}/position")
    def hist_position(session_id: str, driver: str = Query(..., min_length=1), lap: int | None = None, limit: int = Query(3000, ge=1, le=10000)):
        return hist.position(session_id, driver, lap, limit)

    @app.get("/api/historical/sessions/{session_id}/datasets/{name}")
    def hist_dataset(session_id: str, name: str, driver: str | None = None, limit: int = Query(4000, ge=0, le=20000)):
        return hist.dataset(session_id, name, driver, limit)

    @app.get("/api/historical/sessions/{session_id}/weather")
    def hist_weather(session_id: str):
        return hist.weather(session_id)

    @app.get("/api/historical/sessions/{session_id}/track-map")
    def hist_track_map(session_id: str, driver: str | None = None, lap: int | None = None):
        return hist.track_map(session_id, driver, lap)

    @app.post("/api/historical/sessions/{session_id}/compare")
    def hist_compare(session_id: str, req: HistoricalCompareRequest):
        return hist.compare(session_id, req)

    # ------------------------------------------------------------ frontend
    fe = Path(frontend_dir or os.environ.get("F1LAB_FRONTEND_DIR", DEFAULT_FRONTEND))

    @app.get("/api/frontend-build")
    def frontend_build():
        """Fingerprint of the frontend files (names, sizes, modification times). An open browser tab compares it on every page
        change and reloads when it differs, so a tab opened before an update never keeps running old JavaScript."""
        files = sorted(p for p in fe.glob("*") if p.is_file() and p.suffix in (".html", ".js", ".css")) if fe.is_dir() else []
        raw = "|".join(f"{p.name}:{p.stat().st_size}:{p.stat().st_mtime_ns}" for p in files)
        return {"build": hashlib.sha256(raw.encode()).hexdigest()[:16], "files": len(files)}

    if fe.is_dir():
        app.mount("/", _NoCacheStaticFiles(directory=fe, html=True), name="frontend")
    return app
