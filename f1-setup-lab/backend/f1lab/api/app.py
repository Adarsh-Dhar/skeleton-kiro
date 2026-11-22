"""FastAPI application factory for F1 SETUP LAB."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

import trialforge
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError as PydanticValidationError
from trialforge import (
    DesignError,
    ExecutionError,
    InvalidTransitionError,
    OptimizationError,
    TrialforgeError,
)
from trialforge import ValidationError as TFValidationError

from f1lab import NOTICES, SYNTHETIC_NOTICE, __version__
from f1lab.adapters.vehicle_model import FACTORS
from f1lab.api.schemas import (
    METHODS,
    AnalysisRequest,
    ExperimentRequest,
    LiveConnectRequest,
    OptimiseRequest,
    TelemetryRequest,
    default_factors,
)
from f1lab.catalogs.ea_f1_25 import GAME_FACTORS, GAME_ID
from f1lab.catalogs.verification import get_probe_report, list_probe_reports
from f1lab.catalogs.verification import status as verification_status
from f1lab.integrations.game_udp.probe import VARIANT_LABELS
from f1lab.integrations.historical.openf1_client import OpenF1Error
from f1lab.integrations.historical.schemas import HistoricalCompareRequest, HistoricalImportRequest, Provider
from f1lab.integrations.historical.service import HistoricalService
from f1lab.services.lab import Conflict, LabService, NotFound

DEFAULT_FRONTEND = Path(__file__).resolve().parents[3] / "frontend"
METHOD_INFO = {
    "fractional_factorial": "Screening: a fraction of all low/high combinations (aliasing reported). Best first step.",
    "full_factorial": "Every low/high/mid combination of the enabled factors. Most information, most runs.",
    "ofat": "One factor at a time from the baseline. Simple but misses interactions.",
    "central_composite": "Response-surface design (curvature) for 2+ factors. Use after screening.",
    "box_behnken": "Response-surface design without corner runs (3 to 5 factors).",
    "random": "Space-filling random setups within the ranges.",
}  # fmt: skip


def _issue(field: str, message: str) -> dict:
    return {"field": field, "message": message}


class _NoCacheStaticFiles(StaticFiles):
    """The app's own HTML/JS/CSS must never be served from a stale browser cache after an update."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


def create_app(db_path: str | None = None, frontend_dir: str | None = None, fastf1_client_factory=None,
               openf1_client_factory=None) -> FastAPI:
    """The factories and ``db_path`` are for tests; production uses the root database (TRIALFORGE_DB), FastF1 and OpenF1."""
    lab = LabService(db_path)
    hist = HistoricalService(lab.repo.db_engine, lab.data_dir / "fastf1-cache", client_factory=fastf1_client_factory,
                             openf1_client_factory=openf1_client_factory)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.interrupted_imports = hist.recover()
        app.state.recovered = await lab.startup()
        yield
        await hist.shutdown()
        await lab.shutdown()

    app = FastAPI(title="F1 SETUP LAB", version=__version__, lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.lab = lab
    app.state.historical = hist

    # ------------------------------------------------------------ errors
    @app.exception_handler(RequestValidationError)
    async def _req(_: Request, exc: RequestValidationError):
        issues = [_issue(".".join(str(p) for p in e["loc"][1:]) or "request", e["msg"].removeprefix("Value error, ")) for e in exc.errors()]
        return JSONResponse({"detail": issues}, status_code=422)

    @app.exception_handler(PydanticValidationError)
    async def _pyd(_: Request, exc: PydanticValidationError):
        issues = [_issue(".".join(str(p) for p in e["loc"]) or "experiment", e["msg"].removeprefix("Value error, ")) for e in exc.errors()]
        return JSONResponse({"detail": issues}, status_code=422)

    @app.exception_handler(TFValidationError)
    async def _tfv(_: Request, exc: Exception):
        return JSONResponse({"detail": [_issue("experiment", str(exc))]}, status_code=422)

    @app.exception_handler(DesignError)
    async def _design(_: Request, exc: Exception):
        return JSONResponse({"detail": [_issue("design", str(exc))]}, status_code=422)

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
    @app.exception_handler(OptimizationError)
    @app.exception_handler(ExecutionError)
    @app.exception_handler(InvalidTransitionError)
    async def _conflict(_: Request, exc: Exception):
        return JSONResponse({"detail": [_issue("state", str(exc))]}, status_code=409)

    @app.exception_handler(TrialforgeError)
    async def _tf(_: Request, exc: Exception):
        return JSONResponse({"detail": [_issue("sdk", str(exc))]}, status_code=400)

    # ------------------------------------------------------------ meta
    @app.get("/api/health")
    def health():
        return {"status": "ok", "app": "f1-setup-lab", "version": __version__, "trialforge": trialforge.__version__,
                "trialforge_path": str(Path(trialforge.__file__).parent), "recovered": getattr(app.state, "recovered", [])}  # fmt: skip

    @app.get("/api/meta")
    def meta():
        ver = verification_status(lab.repo.db_engine)
        return {
            "notice": SYNTHETIC_NOTICE, "notices": dict(NOTICES),
            "modes": [{"id": "synthetic", "label": "Synthetic simulator"}, {"id": "live_simulator", "label": "Live simulator (EA SPORTS F1 25)"}],
            "game": {"id": GAME_ID, "default_port": 20777, "variants": VARIANT_LABELS,
                     # 'verified' = manually confirmed for every game variant; per-variant status and its source in 'verification'
                     "factors": {k: {**v, "verified": all(ver[var][k]["verified"] for var in ver),
                                     "verification": {var: ver[var][k] for var in ver}} for k, v in GAME_FACTORS.items()},
                     "unsupported_methods": ["central_composite", "box_behnken"]},
            "version": __version__, "trialforge_version": trialforge.__version__,
            "factors": {k: {**v} for k, v in FACTORS.items()},
            "default_factors": {k: s.model_dump() for k, s in default_factors().items()},
            "methods": [{"id": m, "description": METHOD_INFO[m]} for m in METHODS],
            "optimisers": [{"id": "bayesian", "description": "Gaussian-process surrogate + Expected Improvement, constraint-aware"},
                           {"id": "optuna", "description": "Optuna sampler seeded with your results"},
                           {"id": "random", "description": "Random unseen feasible setups"}, {"id": "grid", "description": "Next unseen grid point"}],
            "telemetry_columns": ["lap", "sector_1", "sector_2", "sector_3", "lap_time", "stability", "top_speed", "tyre_degradation"],
        }  # fmt: skip

    # ------------------------------------------------------------ experiments
    @app.get("/api/experiments")
    def list_experiments():
        return lab.list()

    @app.post("/api/experiments", status_code=201)
    def create_experiment(req: ExperimentRequest):
        return lab.create(req)

    @app.get("/api/experiments/{exp_id}")
    def get_experiment(exp_id: str):
        return lab.detail(exp_id)

    @app.get("/api/experiments/{exp_id}/runs")
    def get_runs(exp_id: str):
        return lab.runs(exp_id)

    @app.post("/api/experiments/{exp_id}/execute", status_code=202)
    async def execute(exp_id: str):  # async: the SDK's background runner needs the running event loop
        return lab.execute(exp_id)

    @app.post("/api/experiments/{exp_id}/cancel")
    async def cancel(exp_id: str):
        return await lab.cancel(exp_id)

    @app.post("/api/experiments/{exp_id}/retry", status_code=202)
    async def retry(exp_id: str):
        return lab.retry_failed(exp_id)

    # ------------------------------------------------------------ telemetry
    @app.post("/api/telemetry/preview")
    def telemetry_preview(req: TelemetryRequest):
        return lab.telemetry_preview(req.csv, req.time_unit)

    @app.get("/api/experiments/{exp_id}/telemetry")
    def telemetry(exp_id: str):
        return lab.telemetry(exp_id)

    @app.post("/api/experiments/{exp_id}/runs/{run_id}/telemetry")
    async def attach_telemetry(exp_id: str, run_id: str, req: TelemetryRequest):
        return await lab.attach_telemetry(exp_id, run_id, req.csv, req.time_unit, req.filename)

    # ------------------------------------------------------------ live simulator (UDP)
    @app.get("/api/live/status")
    def live_status():
        return lab.live_status()

    @app.post("/api/live/connect")
    async def live_connect(req: LiveConnectRequest | None = None):
        req = req or LiveConnectRequest()
        return await lab.live_connect(req.host, req.port)

    @app.post("/api/live/disconnect")
    def live_disconnect():
        return lab.live_disconnect()

    @app.get("/api/live/telemetry")
    def live_telemetry(scope: str = "live", limit: int = 1500):
        if scope not in ("live", "stint"):
            raise HTTPException(422, "scope must be 'live' or 'stint'")
        if limit < 1 or limit > 3000:
            raise HTTPException(422, "limit must be between 1 and 3000")
        return lab.live_telemetry(scope, limit)

    @app.post("/api/experiments/{exp_id}/runs/{run_id}/stint/start")
    def stint_start(exp_id: str, run_id: str):
        return lab.stint_start(exp_id, run_id)

    @app.post("/api/experiments/{exp_id}/runs/{run_id}/stint/submit")
    async def stint_submit(exp_id: str, run_id: str):
        return await lab.stint_submit(exp_id, run_id)

    @app.get("/api/experiments/{exp_id}/runs/{run_id}/evidence")
    def evidence(exp_id: str, run_id: str):
        return lab.evidence(exp_id, run_id)

    @app.get("/api/experiments/{exp_id}/telemetry/series-index")
    def telemetry_series_index(exp_id: str):
        return lab.telemetry_series_index(exp_id)

    @app.get("/api/experiments/{exp_id}/runs/{run_id}/telemetry/series.csv")
    def run_telemetry_series_csv(exp_id: str, run_id: str):
        return PlainTextResponse(lab.telemetry_series_csv(exp_id, run_id), media_type="text/csv",
                                 headers={"Content-Disposition": f'attachment; filename="{exp_id}-{run_id}-telemetry.csv"'})

    @app.get("/api/experiments/{exp_id}/runs/{run_id}/telemetry/series")
    def run_telemetry_series(exp_id: str, run_id: str, limit: int = Query(5000, ge=1, le=20000)):
        return lab.run_telemetry_series(exp_id, run_id, limit)

    # ------------------------------------------------------------ historical F1 (FastF1)
    @app.get("/api/historical/events")
    async def hist_events(year: int = Query(2024, ge=2018, le=2100), provider: Provider = "fastf1"):
        return await asyncio.to_thread(hist.events, year, provider)

    @app.get("/api/historical/event-sessions")
    async def hist_event_sessions(year: int = Query(2024, ge=2018, le=2100), event: str = Query(..., min_length=1),
                                  provider: Provider = "fastf1"):
        return await asyncio.to_thread(hist.sessions_for_event, year, event, provider)

    # ------------------------------------------------------------ setup-mapping verification (read-only; confirm with the script)
    @app.get("/api/game/verification")
    def game_verification():
        return {"variants": VARIANT_LABELS, "status": verification_status(lab.repo.db_engine),
                "how_to_confirm": "python scripts/probe_setup.py --guided, then python scripts/verify_setup_mapping.py confirm ..."}

    @app.get("/api/game/probe-reports")
    def game_probe_reports(limit: int = Query(50, ge=1, le=500)):
        return list_probe_reports(lab.repo.db_engine, limit)

    @app.get("/api/game/probe-reports/{report_id}")
    def game_probe_report(report_id: str):
        rep = get_probe_report(lab.repo.db_engine, report_id)
        if rep is None:
            raise NotFound(report_id)
        return rep

    @app.get("/api/historical/openf1/cache")
    def hist_openf1_cache():
        return hist.openf1_cache()

    @app.delete("/api/historical/openf1/cache")
    def hist_openf1_cache_clear():
        """Forget every cached OpenF1 answer (stored sessions are not touched); the next import downloads again."""
        return hist.clear_openf1_cache()

    @app.get("/api/historical/drivers")
    async def hist_drivers(year: int = Query(..., ge=2018, le=2100), event: str = Query(..., min_length=1), session: str = Query(..., min_length=1),
                           provider: Provider = "fastf1"):
        return await hist.drivers_preview(year, event, session, provider)

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

    # ------------------------------------------------------------ analysis / optimisation
    @app.post("/api/experiments/{exp_id}/analysis")
    async def analysis(exp_id: str, req: AnalysisRequest | None = None):
        req = req or AnalysisRequest()
        return await lab.analyse(exp_id, req.alpha, req.bootstrap)

    @app.get("/api/experiments/{exp_id}/recommendation")
    async def recommendation(exp_id: str):
        return await lab.recommendation(exp_id)

    @app.post("/api/experiments/{exp_id}/optimise/suggest")
    async def suggest(exp_id: str, req: OptimiseRequest | None = None):
        req = req or OptimiseRequest()
        return {"recommendations": await lab.optimise_suggest(exp_id, req.count, req.method)}

    @app.post("/api/experiments/{exp_id}/optimise/apply", status_code=202)
    async def apply(exp_id: str, req: OptimiseRequest | None = None):
        req = req or OptimiseRequest()
        return await lab.optimise_apply(exp_id, req.count, req.method)

    @app.get("/api/experiments/{exp_id}/optimise/history")
    def history(exp_id: str):
        return lab.optimisation_history(exp_id)

    # ------------------------------------------------------------ reports & exports
    @app.get("/api/experiments/{exp_id}/report")
    async def report(exp_id: str):
        return await lab.report(exp_id)

    @app.get("/api/experiments/{exp_id}/report.json")
    async def report_json(exp_id: str):
        data = await lab.report(exp_id)
        return Response(json.dumps(data, indent=2), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{exp_id}-report.json"'})  # fmt: skip

    @app.get("/api/experiments/{exp_id}/report.html", response_class=HTMLResponse)
    async def report_html(exp_id: str):
        return HTMLResponse(await lab.report_html(exp_id))

    @app.get("/api/experiments/{exp_id}/export/{kind}.csv")
    def export(exp_id: str, kind: str):
        return PlainTextResponse(lab.csv_export(exp_id, kind), media_type="text/csv",
                                 headers={"Content-Disposition": f'attachment; filename="{exp_id}-{kind}.csv"'})  # fmt: skip

    DATA = Path(os.environ.get("F1LAB_DATA_DIR", Path(__file__).resolve().parents[3] / "data"))

    @app.get("/api/demo/{name}.csv")
    def demo(name: str):
        """Bundled demo telemetry (synthetic, generated by this app's own model)."""
        if name not in ("telemetry", "telemetry_ms") or not (DATA / f"demo_{name}.csv").exists():
            raise NotFound(name)
        return PlainTextResponse((DATA / f"demo_{name}.csv").read_text(), media_type="text/csv")

    # ------------------------------------------------------------ frontend
    fe = Path(frontend_dir or os.environ.get("F1LAB_FRONTEND_DIR", DEFAULT_FRONTEND))
    if fe.is_dir():
        app.mount("/", _NoCacheStaticFiles(directory=fe, html=True), name="frontend")
    return app
