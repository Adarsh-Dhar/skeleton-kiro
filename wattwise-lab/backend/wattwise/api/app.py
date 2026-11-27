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
from wattwise.adapters import provenance as PV
from wattwise.adapters.household_model import FACTORS, METRIC_UNITS, MissingInput, baseline_setup, validate_tariff
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
    ReconcileRequest,
    TariffIn,
    default_factors,
)
from wattwise.services.comparison import build as comparison_build
from wattwise.services.comparison_report import render as comparison_html
from wattwise.services.evidence import EvidenceError
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



class _RevalidatedStaticFiles(StaticFiles):
    """Frontend files with ``Cache-Control: no-cache``: the browser keeps its copy but checks the ETag on every load, so an
    updated app.js is picked up on a normal reload instead of a stale heuristically cached copy being reused."""
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp

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

    for _exc, _field in ((MissingInput, "household"), (PV.ExampleDataError, "source"), (EvidenceError, "evidence"),
                         (BaselineUnavailable, "baseline"), (ProvenanceError, "provenance"), (FactorConflict, "appliance_start"),
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
            "factors": {k: {**v} for k, v in FACTORS.items()},
            "default_factors": {k: s.model_dump() for k, s in default_factors().items()},  # search ranges for the designer, not household data
            "data_policy": "zero fabricated input data: every input is yours or comes from a cited source; results are projections",
            "source_types": {k: PV.SOURCE_TYPE_LABELS[k] for k in PV.SOURCE_TYPES if k != "example"},
            "verification_levels": dict(PV.VERIFICATION_LABELS), "input_units": dict(PV.INPUT_UNITS),
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
    def simulate_day(req: DayRequest):
        return lab.day(req)

    @app.get("/api/experiments/{exp_id}/baseline/profile")
    def baseline_profile(exp_id: str):
        return lab.baseline_profile(exp_id)
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
        return lab.tariffs.save(lab.evidence.bind_all(req.model_dump(mode="json")))
    @app.put("/api/tariffs/{tariff_id}")
    def update_tariff(tariff_id: str, req: TariffIn):
        try:
            return lab.tariffs.save(lab.evidence.bind_all(req.model_dump(mode="json")), tariff_id)
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

    @app.post("/api/experiments/{exp_id}/retire")
    def retire(exp_id: str):
        """Remove an experiment from every list without deleting it (reversible with /unretire)."""
        return lab.retire(exp_id)
    @app.get("/api/experiments-retired")
    def retired():
        return lab.retired()
    @app.post("/api/experiments/{exp_id}/unretire")
    def unretire(exp_id: str):
        return lab.unretire(exp_id)
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
        if not (req.source or "").strip():
            raise ValueError("source is required: say where the file came from (for example your utility's consumer portal)")
        record = lab.evidence.bind(req.meter_source.model_dump(mode="json")) if req.meter_source else None
        return lab.meters.import_csv(req.csv, req.file_name, req.options, req.source.strip(), req.force, req.provenance, record)

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
    def _household_state(state: dict) -> dict:
        """Household state plus what is still missing and the current schedule derived from the owner's inputs."""
        s = state["settings"]
        hh = {"ev": s.get("ev"), "appliances": s.get("appliances") or [], "charger_capacity_kw": s.get("charger_power_kw")}
        return {**state, "readiness": lab.readiness(s), "current_schedule": baseline_setup(hh)}
    @app.get("/api/household")
    def household_get():
        return _household_state(lab.households.get())

    @app.put("/api/household")
    def household_put(req: HouseholdPut):
        settings = HouseholdSettings(**lab.evidence.bind_all(req.model_dump(mode="json", exclude={"migrated_from"})))
        if settings.tariff_id:
            try:
                lab.tariffs.get(settings.tariff_id)  # 404 for an unknown tariff
            except KeyError:
                raise NotFound(f"tariff {settings.tariff_id}") from None
        if settings.baseline_import_id:
            lab.meters.get(settings.baseline_import_id)
        if req.migrated_from == "localStorage":
            state, applied = lab.households.migrate_once(settings, "migrated-localstorage")
            return {**_household_state(state), "migration_applied": applied}
        return _household_state(lab.households.put(settings))

    @app.post("/api/household/reset")
    def household_reset():
        """Clear the configuration (it is kept as the backup). Nothing is filled in: the household is empty afterwards."""
        return _household_state(lab.households.reset())
    @app.post("/api/household/restore")
    def household_restore():
        try:
            return _household_state(lab.households.restore())
        except KeyError:
            raise NotFound("household backup") from None
    # ------------------------------------------------------------ source evidence (documents WATTWISE retrieved or you attached)
    @app.post("/api/evidence/retrieve", status_code=201)
    def evidence_retrieve(body: dict):
        """WATTWISE downloads a public https document (tariff order, spec sheet) and archives it with its SHA-256."""
        return lab.evidence.retrieve(str(body.get("url") or ""))
    @app.post("/api/evidence/attach", status_code=201)
    def evidence_attach(body: dict):
        """Archive a file you supply (for example a PDF of your bill), base64-encoded. It backs user-supplied sources only."""
        return lab.evidence.attach(str(body.get("file_name") or ""), str(body.get("content_b64") or ""), body.get("content_type"))
    @app.get("/api/evidence")
    def evidence_list():
        return lab.evidence.list()
    @app.get("/api/evidence/{evidence_id}")
    def evidence_get(evidence_id: str):
        try:
            return lab.evidence.get(evidence_id)
        except KeyError:
            raise NotFound(f"evidence {evidence_id}") from None
    @app.get("/api/evidence/{evidence_id}/content")
    def evidence_content(evidence_id: str):
        try:
            body, ctype, name = lab.evidence.content(evidence_id)
        except KeyError:
            raise NotFound(f"evidence {evidence_id}") from None
        return Response(body, media_type=ctype or "application/octet-stream", headers={"Content-Disposition": f'attachment; filename="{name}"',
                                                                                     "X-Content-Type-Options": "nosniff"})
    @app.post("/api/reconcile", status_code=201)
    def reconcile(req: ReconcileRequest):
        """Price one billing period from your meter readings with your tariff and compare it with your real bill."""
        from wattwise.services.reconciliation import reconcile as run
        return run(lab, req)
    @app.get("/api/reconciliations")
    def reconciliations():
        from wattwise.services.reconciliation import list_all
        return list_all(lab)
    @app.get("/api/audit")
    def audit():
        """Every stored input checked against the data policy: what is blocked, what still needs confirming, what is unverified."""
        from wattwise.services.audit import run as audit_run
        return audit_run(lab)
    @app.get("/api/inputs/export.json")
    def inputs_export():
        """Every configured input with its source and status: household, tariffs and meter imports."""
        state = _household_state(lab.households.get())
        s = state["settings"]
        rows = []
        for t in lab.tariffs.list():
            rows.append(PV.entry(f"tariff[{t['id']}]", t.get("source"), {k: t.get(k) for k in ("name", "utility", "consumer_category", "currency", "periods",
                                                                                              "demand_charge_per_kw", "fixed_daily_charge", "fixed_monthly_charge",
                                                                                              "tiers", "surcharge_per_kwh", "tax_pct", "export_credit_per_kwh")
                                                                       if t.get(k) is not None}))
        if s.get("ev"):
            ev = s["ev"]
            rows.append(PV.entry("ev", ev.get("source"), {k: ev.get(k) for k in ("battery_kwh", "efficiency", "initial_soc_pct", "desired_charge_pct",
                                                                                 "departure_hour", "plug_in_hour")}))
        if s.get("charger_power_kw") is not None:
            rows.append(PV.entry("charger", s.get("charger_source"), {"charger_power_kw": s["charger_power_kw"]}))
        for a in s.get("appliances") or []:
            rows.append(PV.entry(f"appliances[{a['name']}]", a.get("source"), {k: a.get(k) for k in ("power_kw", "duration_h", "flexible", "fixed_start")}))
        if s.get("base_load_kw") is not None:
            rows.append(PV.entry("base_load", s.get("base_load_source"), {"base_load_kw": s["base_load_kw"]}))
        for m in lab.meters.list():
            rows.append({"input": f"meter_import[{m['import_id']}]", "values": {k: m[k] for k in ("file_name", "first_utc", "last_utc", "rows_used",
                                                                                                  "interval_minutes", "total_import_kwh")},
                         "units": {"total_import_kwh": "kWh", "interval_minutes": "min"}, "source": m["source_record"], "status": m["source_status"]})
        return {"exported_at": date.today().isoformat(), "data_policy": "zero fabricated input data", "readiness": state["readiness"],
                "household_saved": state["saved"], "inputs": rows}

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

    # ------------------------------------------------------------ frontend
    fe = Path(frontend_dir or os.environ.get("WATTWISE_FRONTEND_DIR", DEFAULT_ROOT / "frontend"))
    if fe.is_dir():
        app.mount("/", _RevalidatedStaticFiles(directory=fe, html=True), name="frontend")
    return app
