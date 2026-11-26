"""FastAPI application factory for WATTWISE LAB.

Every capability of the lab is reachable over HTTP; the browser frontend (``frontend/``) is just a client of these routes.
"""

from __future__ import annotations

import importlib.util
import json
import os
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

import trialforge
from fastapi import FastAPI, Query, Request
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

from wattwise import SIMULATED_NOTICE, __version__
from wattwise.adapters.household_model import BASELINE, DEFAULT_APPLIANCES, DEFAULT_EV, FACTORS, METRIC_UNITS, validate_tariff
from wattwise.api.schemas import (
    METHODS,
    AnalysisRequest,
    DayRequest,
    ExperimentRequest,
    HouseholdPut,
    HouseholdSettings,
    MeterAttachRequest,
    MeterImportRequest,
    MeterRequest,
    OptimiseRequest,
    TariffIn,
    default_factors,
)
from wattwise.services.comparison import build as comparison_build
from wattwise.services.comparison_report import render as comparison_html
from wattwise.services.lab import (
    BaselineOverlap,
    BaselineUnavailable,
    ChargerCapacityError,
    Conflict,
    FactorConflict,
    InvalidDayType,
    LabService,
    MissingStartDate,
    NoCompleteDay,
    NotFound,
)
from wattwise.services.meter_library import PROVENANCE_LABELS, ProvenanceError, provenance_badge
from wattwise.services.meter_library import preview as meter_preview_fn
from wattwise.services.tariffs import TariffError

DEFAULT_ROOT = Path(__file__).resolve().parents[3]
METHOD_INFO = {
    "full_factorial": "Every combination of the chosen levels of each schedule variable. Most information; run count grows quickly.",
    "fractional_factorial": "A fraction of the full grid (aliasing is reported). Needs three or more factors to save runs.",
    "ofat": "One variable at a time from today's schedule. Simple, but cannot see interactions between variables.",
    "central_composite": "Response-surface design that can detect curvature. Use after a first screening round.",
    "box_behnken": "Response-surface design without corner runs (three or more factors).",
    "random": "Space-filling random schedules inside the ranges. Useful when constraints make a grid awkward.",
}


def _issue(field: str, message: str) -> dict:
    return {"field": field, "message": message}


def optuna_available() -> bool:
    """True when Optuna can be imported (the 'optuna' optimiser needs it)."""
    return importlib.util.find_spec("optuna") is not None
def available_optimisers() -> list[dict[str, str]]:
    opts = [{"id": "bayesian", "description": "Gaussian-process surrogate + Expected Improvement, constraint-aware"}]
    if optuna_available():
        opts.append({"id": "optuna", "description": "Optuna sampler seeded with your results"})
    opts += [{"id": "random", "description": "Random unseen feasible schedules"}, {"id": "grid", "description": "Next unseen grid point"}]
    return opts
def create_app(db_path: str | None = None, frontend_dir: str | None = None, data_dir: str | None = None) -> FastAPI:
    lab = LabService(db_path)  # db_path is for tests; production uses the root database (TRIALFORGE_DB)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.recovered = await lab.startup()  # re-registers adapters and resumes interrupted runs from the database
        yield
        await lab.shutdown()

    app = FastAPI(title="WATTWISE LAB", version=__version__, lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.lab = lab

    # ------------------------------------------------------------ errors (one consistent {"detail": [{field, message}]} shape)
    @app.exception_handler(RequestValidationError)
    async def _req(_: Request, exc: RequestValidationError):
        issues = [_issue(".".join(str(p) for p in e["loc"][1:]) or "request", e["msg"].removeprefix("Value error, ")) for e in exc.errors()]
        return JSONResponse({"detail": issues}, status_code=422)

    @app.exception_handler(PydanticValidationError)
    async def _pyd(_: Request, exc: PydanticValidationError):
        issues = [_issue(".".join(str(p) for p in e["loc"]) or "experiment", e["msg"].removeprefix("Value error, ")) for e in exc.errors()]
        return JSONResponse({"detail": issues}, status_code=422)

    @app.exception_handler(TariffError)
    async def _tariff(_: Request, exc: TariffError):
        return JSONResponse({"detail": [_issue("tariff", i) for i in exc.issues]}, status_code=422)

    @app.exception_handler(TFValidationError)
    async def _tfv(_: Request, exc: Exception):
        return JSONResponse({"detail": [_issue("experiment", str(exc))]}, status_code=422)

    @app.exception_handler(DesignError)
    async def _design(_: Request, exc: Exception):
        return JSONResponse({"detail": [_issue("design", str(exc))]}, status_code=422)

    @app.exception_handler(BaselineOverlap)
    async def _overlap(_: Request, exc: BaselineOverlap):
        body: dict = {"detail": [_issue("baseline", str(exc))]}
        if getattr(exc, "baseline_check", None) is not None:
            body["baseline_check"] = exc.baseline_check
        return JSONResponse(body, status_code=422)

    def _field_error(field: str):
        async def handler(_: Request, exc: Exception):
            return JSONResponse({"detail": [_issue(field, str(exc))]}, status_code=422)

        return handler

    for _exc, _field in ((BaselineUnavailable, "baseline"), (ProvenanceError, "provenance"), (FactorConflict, "appliance_start"),
                         (ChargerCapacityError, "charger_capacity_kw"), (MissingStartDate, "window_start_date"),
                         (NoCompleteDay, "baseline"), (InvalidDayType, "day_type")):
        app.add_exception_handler(_exc, _field_error(_field))

    @app.exception_handler(ValueError)
    async def _val(_: Request, exc: ValueError):
        return JSONResponse({"detail": [_issue("request", str(exc))]}, status_code=422)

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
        return {"status": "ok", "app": "wattwise-lab", "version": __version__, "trialforge": trialforge.__version__,
                "trialforge_path": str(Path(trialforge.__file__).parent), "recovered": getattr(app.state, "recovered", [])}  # fmt: skip

    @app.get("/api/meta")
    def meta():
        return {
            "notice": SIMULATED_NOTICE, "version": __version__, "trialforge_version": trialforge.__version__,
            "factors": {k: {**v} for k, v in FACTORS.items()}, "baseline": dict(BASELINE),
            "default_factors": {k: s.model_dump() for k, s in default_factors().items()},
            "default_ev": dict(DEFAULT_EV), "default_appliances": [dict(a) for a in DEFAULT_APPLIANCES],
            "metrics": dict(METRIC_UNITS), "tariffs": lab.tariffs.list(),
            "methods": [{"id": m, "description": METHOD_INFO[m]} for m in METHODS],
            "optimisers": available_optimisers(),
            "provenance_badges": {k: provenance_badge(k) for k in PROVENANCE_LABELS},
            "meter_columns": ["timestamp", "power_kw | power_w | power"],
        }  # fmt: skip

    # ------------------------------------------------------------ dashboard & simulation
    @app.get("/api/overview")
    def overview():
        return lab.overview()

    @app.post("/api/simulate/day")
    def simulate_day(req: DayRequest | None = None):
        return lab.day(req or DayRequest())

    @app.get("/api/experiments/{exp_id}/runs/{run_id}/profile")
    def run_profile(exp_id: str, run_id: str):
        return lab.run_profile(exp_id, run_id)

    # ------------------------------------------------------------ tariffs
    @app.get("/api/tariffs")
    def list_tariffs():
        return lab.tariffs.list()

    @app.post("/api/tariffs/validate")
    def validate(tariff: dict):
        issues = validate_tariff(tariff)
        return {"valid": not issues, "issues": issues}

    @app.post("/api/tariffs", status_code=201)
    def create_tariff(req: TariffIn):
        return lab.tariffs.save(req.model_dump())

    @app.put("/api/tariffs/{tariff_id}")
    def update_tariff(tariff_id: str, req: TariffIn):
        try:
            return lab.tariffs.save(req.model_dump(), tariff_id)
        except KeyError:
            raise NotFound(f"tariff {tariff_id}") from None

    @app.delete("/api/tariffs/{tariff_id}", status_code=204)
    def delete_tariff(tariff_id: str):
        try:
            lab.tariffs.delete(tariff_id)
        except KeyError:
            raise NotFound(f"tariff {tariff_id}") from None
        return Response(status_code=204)

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

    # ------------------------------------------------------------ smart-meter CSV
    @app.post("/api/meter/preview")
    def meter_preview(req: MeterRequest):
        return lab.meter_preview(req)

    @app.post("/api/experiments/{exp_id}/runs/{run_id}/meter")
    async def attach_meter(exp_id: str, run_id: str, req: MeterAttachRequest):
        return await lab.attach_meter(exp_id, run_id, req)

    # ------------------------------------------------------------ historical meter-data library (measured usage, not run results)
    @app.post("/api/meter/import/preview")
    def meter_import_preview(req: MeterImportRequest):
        return meter_preview_fn(req.csv, req.options)

    @app.post("/api/meter/import", status_code=201)
    def meter_import(req: MeterImportRequest):
        return lab.meters.import_csv(req.csv, req.file_name, req.options, req.source, req.force, req.provenance)

    @app.get("/api/meter/imports")
    def meter_imports():
        return lab.meters.list()

    @app.get("/api/meter/imports/{import_id}")
    def meter_import_detail(import_id: str):
        return lab.meters.get(import_id)

    @app.get("/api/meter/imports/{import_id}/readings")
    def meter_import_readings(import_id: str, offset: int = Query(0, ge=0), limit: int = Query(5000, ge=1, le=20000)):
        return lab.meters.readings_view(import_id, offset, limit)

    @app.get("/api/meter/imports/{import_id}/summary")
    def meter_import_summary(import_id: str, tariff_id: str | None = None):
        return lab.meter_baseline(import_id, tariff_id)

    @app.get("/api/meter/imports/{import_id}/calibration")
    def meter_import_calibration(import_id: str, day_type: str | None = None):
        return lab.calibration(import_id, day_type)

    @app.get("/api/meter/imports/{import_id}/billing-cycle-default")
    def meter_import_billing_cycle_default(import_id: str, day: date):
        return lab.billing_cycle_default(import_id, day)

    @app.delete("/api/meter/imports/{import_id}")
    def meter_import_delete(import_id: str):
        return lab.meters.delete(import_id)

    @app.get("/api/comparison")
    def comparison(import_id: str, exp_id: str):
        return comparison_build(lab, import_id, exp_id)

    @app.get("/api/comparison/report.html", response_class=HTMLResponse)
    def comparison_report(import_id: str, exp_id: str):
        return comparison_html(comparison_build(lab, import_id, exp_id))

    # ------------------------------------------------------------ household settings (server-side; replaces browser storage)
    @app.get("/api/household")
    def household_get():
        return lab.households.get()

    @app.put("/api/household")
    def household_put(req: HouseholdPut):
        settings = HouseholdSettings(**req.model_dump(exclude={"migrated_from"}))
        lab.resolve_tariff(settings.tariff_id, None)  # 404 for an unknown tariff
        if settings.baseline_import_id:
            lab.meters.get(settings.baseline_import_id)
        if req.migrated_from == "localStorage":
            state, applied = lab.households.migrate_once(settings, "migrated-localstorage")
            return {**state, "migration_applied": applied}
        return lab.households.put(settings)

    @app.post("/api/household/reset")
    def household_reset():
        return lab.households.reset()

    # ------------------------------------------------------------ analysis / optimisation
    @app.post("/api/experiments/{exp_id}/analysis")
    async def analysis(exp_id: str, req: AnalysisRequest | None = None):
        req = req or AnalysisRequest()
        return await lab.analyse(exp_id, req.alpha, req.bootstrap)

    @app.get("/api/experiments/{exp_id}/recommendation")
    async def recommendation(exp_id: str):
        return (await lab.analyse(exp_id))["recommendation"]

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

    # ------------------------------------------------------------ bundled demo meter files
    data = Path(data_dir or os.environ.get("WATTWISE_DATA_DIR", DEFAULT_ROOT / "data"))

    @app.get("/api/demo/{name}.csv")
    def demo(name: str):
        """Bundled demo smart-meter files (synthetic; generated by this app's own household model)."""
        if name not in ("meter", "meter_w") or not (data / f"demo_{name}.csv").exists():
            raise NotFound(name)
        return PlainTextResponse((data / f"demo_{name}.csv").read_text(), media_type="text/csv")

    # ------------------------------------------------------------ frontend
    fe = Path(frontend_dir or os.environ.get("WATTWISE_FRONTEND_DIR", DEFAULT_ROOT / "frontend"))
    if fe.is_dir():
        app.mount("/", StaticFiles(directory=fe, html=True), name="frontend")
    return app
