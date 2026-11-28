"""LabService: the F1 workflow on top of the TRIALFORGE SDK.

The SDK does all experiment work (design, execution, measurements, statistics, optimisation, persistence, reports);
this class only maps the application's concepts (setup factors, stability limit, telemetry) onto SDK objects.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import text
from trialforge import (
    AdapterRegistry,
    Constraint,
    ExecutionPolicy,
    ExperimentResult,
    ExperimentService,
    ExperimentSpec,
    ExperimentStatus,
    Factor,
    LocalBackgroundRunner,
    Measurement,
    MeasurementDefinition,
    MeasurementEngine,
    Objective,
    ReportEngine,
    RunStatus,
)

from db.config import resolve_db_url, sqlite_file_path
from db.models.common import APP_F1
from db.scoped import ScopedExperimentRepository
from f1lab import LABEL_IMPORTED, LABEL_MIXED, LABEL_SYNTHETIC, notice_for, provenance_notice
from f1lab.adapters.sim_adapter import F1SimulatorAdapter
from f1lab.adapters.telemetry import _CANON, EXTRA, SECTORS, UNAVAILABLE_TIMESTAMP, parse_telemetry_csv, to_measurements
from f1lab.adapters.vehicle_model import BASELINE, FACTORS, METRIC_UNITS
from f1lab.api.schemas import ExperimentRequest
from f1lab.integrations.historical.providers import PROVENANCE_DEMO, PROVENANCE_USER
from f1lab.services import narrative, report_html

ADAPTER_PREFIX = "f1-sim/"
GENERATORS = {4: ["D=ABC"], 3: ["C=AB"]}  # half fractions of the enabled factors (resolution IV and III)
NOTABLE = {"unit_converted": "unit converted", "statistical_outlier": "outlier", "sensor_warning": "sensor warning", "timestamp_unavailable": "no timestamp"}


def mode_of(spec: ExperimentSpec) -> str:
    return spec.metadata.get("mode", "synthetic")


def _run_imported(r) -> bool:
    """True if any measurement of the run came from an imported CSV."""
    return any(m.metadata.get("imported") for m in r.measurements)



# Which design settings each method uses. Anything else the user typed is ignored by the SDK designer and is reported as such.
DESIGN_CONTROLS: dict[str, tuple[str, ...]] = {
    "factorial": ("replicates",), "ofat": ("replicates",), "fractional_factorial": ("replicates", "center_points"),
    "central_composite": ("center_points",), "box_behnken": ("center_points",), "random": ("n_runs",),
}  # fmt: skip

class NotFound(KeyError):
    pass


class Conflict(RuntimeError):
    pass


class MissingSourceField(ValueError):
    """A measurement cannot be created because a source-file fact is unavailable (Req 13.8). ``field`` names it."""

    def __init__(self, field: str) -> None:
        super().__init__(f"cannot create measurements: {field} is unavailable")
        self.field = field


class SetupLinkError(ValueError):
    """A Setup_Link submission was rejected before anything was written. ``missing`` names factors without a value or statement."""

    def __init__(self, message: str, missing: list[str] | None = None) -> None:
        super().__init__(message)
        self.missing = missing or []


SETUP_LINK_MAX_LAPS, SETUP_LINK_MAX_METRICS = 100, 50
SETUP_LINK_LAP_METRICS = {"lap_time": "lap_time_s", "sector_1": "sector_1_s", "sector_2": "sector_2_s", "sector_3": "sector_3_s"}
SETUP_LINK_METRICS = (*SETUP_LINK_LAP_METRICS, "top_speed")
UNVERIFIED_FLAG = "unverified_setup"


def source_facts(csv_text: str, time_unit: str, filename: str | None) -> dict[str, Any]:
    """Original filename, file hash, Column_Mapping and units of a CSV (Req 13.7). Unavailable values are None/empty."""
    header = next(csv.reader(io.StringIO(csv_text.lstrip("\ufeff"))), [])
    names = {h.strip().lower(): h.strip() for h in header if h and h.strip()}
    mapping = {k: names[k] for k in (*_CANON, "timestamp", *EXTRA) if k in names}
    return {
        "original_filename": (filename or "").strip() or None,
        "file_hash": hashlib.sha256(csv_text.encode()).hexdigest() if csv_text else None,
        "column_mapping": mapping, "units": {"time": "ms" if time_unit == "ms" else "seconds"} if time_unit else {},
    }


def require_source_facts(facts: dict[str, Any]) -> None:
    for name in ("original_filename", "file_hash", "column_mapping", "units"):
        if not facts.get(name):
            raise MissingSourceField(name)


def measurement_definitions() -> list[MeasurementDefinition]:
    defs = [MeasurementDefinition(metric=m, unit=u, required=m not in ("top_speed", "tyre_degradation")) for m, u in METRIC_UNITS.items()]
    return defs


class LabService:
    """Owns the SDK service, the per-experiment adapters and the background runner."""

    def __init__(self, db_path: str | None = None, data_dir: str | None = None) -> None:
        """``db_path`` is for tests only; production always uses the root database (``TRIALFORGE_DB`` / ``data/trialforge.db``)."""
        url = resolve_db_url(db_path)
        file = sqlite_file_path(url)
        self.data_dir = Path(data_dir) if data_dir else (file.parent if file else Path("data"))
        self.registry = AdapterRegistry()
        self.repo = ScopedExperimentRepository(APP_F1, url)
        self.svc = ExperimentService(registry=self.registry, repository=self.repo)
        self.runner = LocalBackgroundRunner(self.svc)

    # ------------------------------------------------------------------ lifecycle
    async def startup(self) -> list[dict]:
        """Re-register adapters for stored experiments and recover runs interrupted by a restart."""
        for exp_id in self.repo.list_ids():
            self._register_adapter(self.repo.get_spec(exp_id))
        return await self.svc.recover()

    def _register_adapter(self, spec: ExperimentSpec) -> None:
        if spec.adapter_name and spec.adapter_name not in self.registry:
            cfg = spec.metadata.get("lab", {})
            self.registry.register(
                F1SimulatorAdapter(
                    spec.adapter_name,
                    latency_seconds=cfg.get("latency_seconds", 0.0),
                    fault_rate=cfg.get("fault_rate", 0.0),
                    stint_laps=cfg.get("stint_laps", 5),
                )  # fmt: skip
            )

    async def shutdown(self) -> None:
        await self.runner.shutdown()
        await self.registry.shutdown_all()

    # ------------------------------------------------------------------ creation
    def build_spec(self, req: ExperimentRequest, exp_id: str) -> ExperimentSpec:
        return self._build_synthetic_spec(req, exp_id)

    @staticmethod
    def design_settings_applied(req: ExperimentRequest) -> dict[str, Any]:
        """Which of the replicate, centre-point and run-count settings this design method actually uses, and the values applied."""
        d = req.design
        method = {"full_factorial": "factorial"}.get(d.method, d.method)
        uses = DESIGN_CONTROLS.get(method, ())
        values = {"replicates": d.replicates, "center_points": max(d.center_points, 1) if method in ("central_composite", "box_behnken") else d.center_points,
                  "n_runs": min(d.n_runs, d.run_budget)}  # fmt: skip
        return {"applied": {k: values[k] for k in uses}, "ignored": {k: values[k] for k in ("replicates", "center_points", "n_runs") if k not in uses}}

    @staticmethod
    def _design_options(req: ExperimentRequest, k: int) -> tuple[str, dict[str, Any]]:
        d = req.design
        method = {"full_factorial": "factorial"}.get(d.method, d.method)
        options: dict[str, Any] = {"replicates": d.replicates}
        if method == "fractional_factorial":
            options["center_points"] = d.center_points
            if k in GENERATORS:
                options["generators"] = GENERATORS[k]
        elif method in ("central_composite", "box_behnken"):
            options["center_points"] = max(d.center_points, 1)
            options.pop("replicates")
        elif method == "random":
            options = {"n_runs": min(d.n_runs, d.run_budget)}
        return method, options

    def _build_synthetic_spec(self, req: ExperimentRequest, exp_id: str) -> ExperimentSpec:
        factors = []
        for name, s in req.factors.items():
            if not s.enabled:
                continue
            base = BASELINE[name]
            factors.append(Factor(name=name, factor_type="continuous", lower=s.low, upper=s.high, unit=FACTORS[name]["unit"],
                                  default=base if s.low <= base <= s.high else round((s.low + s.high) / 2, 6)))  # fmt: skip
        k = len(factors)
        d = req.design
        method, options = self._design_options(req, k)
        objectives = [Objective(metric="lap_time", direction="minimize")]
        if req.also_minimise_tyre_wear:
            objectives.append(Objective(metric="tyre_degradation", direction="minimize", weight=0.5))
        return ExperimentSpec(
            experiment_id=exp_id, name=req.name, hypothesis=req.hypothesis,
            description="F1 SETUP LAB experiment on a SYNTHETIC vehicle model.",
            factors=factors, objectives=objectives,
            constraints=[Constraint(metric="stability", kind="output", minimum=req.min_stability)],
            measurement_definitions=measurement_definitions(), design_method=method, design_options=options,
            adapter_name=ADAPTER_PREFIX + exp_id, run_budget=d.run_budget, random_seed=d.seed,
            execution=ExecutionPolicy(max_retries=req.simulator.max_retries, max_concurrency=4, run_timeout_seconds=120),
            metadata={"app": "f1-setup-lab", "synthetic": True, "lab": req.simulator.model_dump(), "min_stability": req.min_stability,
                      "design_settings": self.design_settings_applied(req)},
        )  # fmt: skip

    def create(self, req: ExperimentRequest) -> dict:
        """Validate, register the experiment's simulator, generate the design. Raises SDK ValidationError/DesignError."""
        exp_id = req.experiment_id or "f1-" + uuid.uuid4().hex[:8]
        if self.repo.has_experiment(exp_id) or self.repo.exists_anywhere(exp_id):
            raise Conflict(f"an experiment with id '{exp_id}' already exists")
        spec = self.build_spec(req, exp_id)
        self._register_adapter(spec)
        try:
            self.svc.design(spec)
        except Exception:
            if spec.adapter_name:
                self.registry.unregister(spec.adapter_name)
            raise
        return self.detail(exp_id)

    # ------------------------------------------------------------------ views
    def _result(self, exp_id: str) -> ExperimentResult:
        try:
            return self.svc.get(exp_id)
        except KeyError:
            raise NotFound(exp_id) from None

    @staticmethod
    def _run_row(r) -> dict:
        out = narrative.run_outcomes(r) if r.status == RunStatus.COMPLETED else {}
        imported = _run_imported(r)
        return {
            "run_id": r.run_id, "order": r.order, "status": r.status.value, "configuration": r.configuration,
            "coded": r.coded, "replicate": r.replicate, "attempts": r.attempts, "error": r.error,
            "outcomes": {k: round(v, 4) for k, v in out.items()},
            "feasible": r.metadata.get("feasible") if r.status == RunStatus.COMPLETED else None,
            "point_kind": r.metadata.get("point_kind") or ("center" if r.metadata.get("center_point") else None),
            "iteration": r.metadata.get("sequential_iteration"), "imported": imported,
            "source_label": LABEL_IMPORTED if imported else LABEL_SYNTHETIC,
            "lap_count": len({m.metadata["lap"] for m in r.measurements if "lap" in m.metadata}),
            "sector_count": sum(1 for m in r.measurements if m.metric in SECTORS),
            "quality_warnings": r.metadata.get("quality_warnings", [])[:5],
        }  # fmt: skip

    def _summary(self, res: ExperimentResult) -> dict:
        counts = {s.value: 0 for s in RunStatus}
        for r in res.runs:
            counts[r.status.value] += 1
        rows = [self._run_row(r) for r in res.runs]
        feasible = [r for r in rows if r["status"] == "completed" and r["feasible"] and "lap_time" in r["outcomes"]]
        best = min(feasible, key=lambda r: r["outcomes"]["lap_time"]) if feasible else None
        spec = res.spec
        return {
            "id": spec.experiment_id, "name": spec.name, "hypothesis": spec.hypothesis, "status": res.status.value,
            "created_at": spec.created_at.isoformat(), "design_method": res.plan.method, "run_budget": spec.run_budget,
            "runs_total": len(res.runs), "counts": counts, "min_stability": spec.metadata.get("min_stability"),
            "mode": mode_of(spec),
            "factors": [{"name": f.name, "low": f.lower if f.lower is not None else min(f.values), "high": f.upper if f.upper is not None else max(f.values),
                         "values": f.values, "unit": f.unit, "label": FACTORS.get(f.name, {}).get("label", f.name)}
                        for f in spec.factors],
            "best": {"run_id": best["run_id"], "lap_time": best["outcomes"]["lap_time"], "stability": best["outcomes"].get("stability"),
                     "configuration": best["configuration"]} if best else None,
            "multi_objective": len(spec.objectives) > 1,
        }  # fmt: skip

    def list(self) -> list[dict]:
        out = []
        for i in self.repo.list_ids():
            try:
                out.append(self._summary(self._result(i)))
            except NotFound:  # an experiment without a design (e.g. interrupted creation) is not listed
                continue
        return sorted(out, key=lambda s: s["created_at"], reverse=True)

    @staticmethod
    def _source_counts(res: ExperimentResult) -> dict[str, int]:
        done = [r for r in res.runs if r.status == RunStatus.COMPLETED]
        imported = sum(1 for r in done if _run_imported(r))
        return {"synthetic": len(done) - imported, "imported": imported}

    @classmethod
    def _provenance(cls, res: ExperimentResult) -> str:
        """Where the results of the COMPLETED runs came from. Runs that are queued, failed or cancelled are not considered."""
        c = cls._source_counts(res)
        if c["imported"] and c["synthetic"]:
            return LABEL_MIXED
        if c["imported"]:
            return LABEL_IMPORTED
        if c["synthetic"]:
            return LABEL_SYNTHETIC
        notice_for(mode_of(res.spec))  # no completed runs: the mode decides, and an unknown mode is an error
        return LABEL_SYNTHETIC

    def detail(self, exp_id: str) -> dict:
        res = self._result(exp_id)
        d = self._summary(res)
        d["runs"] = [self._run_row(r) for r in res.runs]
        d["design"] = self.design_view(res)
        d["simulator"] = res.spec.metadata.get("lab", {})
        d["synthetic_notice"] = notice_for(mode_of(res.spec))
        label = self._provenance(res)
        d["notice"] = provenance_notice(label)
        d["source_label"] = label
        d["source_counts"] = self._source_counts(res)
        return d

    def design_view(self, res: ExperimentResult) -> dict:
        info = res.plan.design_info
        keep = ("type", "k", "p", "runs", "generators", "defining_relation", "resolution", "alias_structure", "factor_letters",
                "replicates", "center_points", "complete", "limitations", "alpha", "inscribed", "full_grid_size", "base_factors")  # fmt: skip
        uses = DESIGN_CONTROLS.get(res.plan.method, ())
        labels = {"replicates": "replicates", "center_points": "centre points", "n_runs": "run count"}
        saved = res.spec.metadata.get("design_settings") or {}
        ignored = saved.get("ignored") or {}
        not_used = [k for k in labels if k not in uses]
        summary = f"{res.plan.method.replace('_', ' ')} design: {len(res.plan.runs)} runs. Uses " + (", ".join(labels[k] for k in uses) or "no extra settings") + "."
        if not_used:
            summary += " Does not use " + ", ".join(labels[k] for k in not_used) + (
                "; any values entered for them are not used." if any(ignored.get(k) for k in not_used) else ".")
        if res.plan.method == "random":
            summary += " A random design has no replicated runs and no centre points, so pure error cannot be estimated from it."
        return {
            "settings_used": list(uses), "settings_not_used": not_used, "settings_ignored": ignored, "settings_summary": summary,
            "method": res.plan.method, "seed": res.plan.random_seed, "runs_planned": len(res.plan.runs),
            "randomization": res.plan.randomization.get("scheme"),
            **{k: info[k] for k in keep if k in info},
            "control": res.plan.control_configuration,
        }  # fmt: skip

    def runs(self, exp_id: str) -> list[dict]:
        return [self._run_row(r) for r in self._result(exp_id).runs]

    # ------------------------------------------------------------------ execution
    def execute(self, exp_id: str) -> dict:
        res = self._result(exp_id)
        if not any(r.status in (RunStatus.QUEUED, RunStatus.CREATED) for r in res.runs):
            raise Conflict("no queued runs to execute")
        self._register_adapter(res.spec)
        self.runner.submit(exp_id)
        return {"started": True, "queued": sum(r.status == RunStatus.QUEUED for r in res.runs)}

    async def cancel(self, exp_id: str) -> dict:
        self._result(exp_id)
        res = await self.svc.cancel(exp_id)
        return self._summary(res)

    def retry_failed(self, exp_id: str) -> dict:
        res = self._result(exp_id)
        failed = [r for r in res.runs if r.status == RunStatus.FAILED]
        if not failed:
            raise Conflict("no failed runs to retry")
        for r in failed:
            self.svc.retry_run(exp_id, r.run_id)
        self._register_adapter(res.spec)
        self.runner.submit(exp_id)
        return {"retried": len(failed)}

    # ------------------------------------------------------------------ telemetry
    def telemetry_preview(self, csv_text: str, time_unit: str = "s", provenance_status: str = PROVENANCE_USER) -> dict:
        """Parse a CSV and use the SDK's MeasurementEngine to convert units and flag outlier laps (nothing is deleted)."""
        parsed = parse_telemetry_csv(csv_text, time_unit)
        out: dict[str, Any] = {"errors": parsed.errors, "laps": [], "summary": None, "time_unit": time_unit}
        if not parsed.laps:
            return out
        ms = to_measurements("preview", parsed, "preview", provenance_status)
        eng = MeasurementEngine(measurement_definitions())
        _, assessed, warnings = eng.assess(ms, "preview")
        by_lap: dict[int, dict] = {}
        for m in assessed:
            lap = by_lap.setdefault(m.metadata["lap"], {"lap": m.metadata["lap"], "flags": {}})
            lap[m.metric] = round(float(m.value), 4)
            notable = [NOTABLE.get(f, f) for f in m.quality_flags if f not in ("run_id_bound", "timestamp_assumed_utc", "timestamp_unavailable")]
            if notable:
                lap["flags"][m.metric] = notable
        laps = [by_lap[k] for k in sorted(by_lap)]
        for lap in laps:
            lap["outlier"] = "outlier" in " ".join(
                lap["flags"].get("lap_time", [])
            )  # lap flagged by its lap time; sector flags stay as tags
        times = [lap["lap_time"] for lap in laps if "lap_time" in lap]
        out.update(laps=laps, warnings=warnings, summary={
            "laps": len(laps), "best_lap": min(times), "mean_lap": sum(times) / len(times),
            "outlier_laps": [lap["lap"] for lap in laps if lap["outlier"]],
            "unit_converted": any("unit_converted" in m.quality_flags for m in assessed),
        })  # fmt: skip
        return out

    def import_session(self, csv_text: str, time_unit: str, filename: str, provenance_status: str = PROVENANCE_USER) -> dict:
        """Session import: parse and validate a CSV and describe the session. Creates no experiment attachment (Req 14.5)."""
        parsed = parse_telemetry_csv(csv_text, time_unit)
        if not parsed.laps:
            raise ValueError("; ".join(parsed.errors) or "no usable laps in the file")
        facts = source_facts(csv_text, time_unit, filename)
        require_source_facts(facts)
        return {**facts, "provenance_status": provenance_status, "synthetic": provenance_status == PROVENANCE_DEMO,
                "lap_count": len(parsed.laps), "sector_count": len(parsed.laps) * len(SECTORS), "row_errors": parsed.errors}

    async def attach_telemetry(self, exp_id: str, run_id: str, csv_text: str, time_unit: str, filename: str,
                               provenance_status: str = PROVENANCE_USER) -> dict:
        """Explicit attach of a CSV's laps and sectors to one run. Nothing is stored unless every source fact is available (Req 13.8)."""
        res = self._result(exp_id)
        run = next((r for r in res.runs if r.run_id == run_id), None)
        if run is None:
            raise NotFound(run_id)
        parsed = parse_telemetry_csv(csv_text, time_unit)
        if not parsed.laps:
            raise ValueError("; ".join(parsed.errors) or "no usable laps in the file")
        facts = source_facts(csv_text, time_unit, filename)
        require_source_facts(facts)
        ms = to_measurements(run_id, parsed, f"csv:{filename}", provenance_status)
        for m in ms:
            m.metadata.update(facts)
        updated = await self.svc.ingest_measurements(exp_id, run_id, ms)
        return {"run": self._run_row(updated), "row_errors": parsed.errors,
                "lap_count": len(parsed.laps), "sector_count": len(parsed.laps) * len(SECTORS)}

    def telemetry(self, exp_id: str) -> list[dict]:
        """Per-run laps/sectors from stored measurements, with the SDK's quality flags."""
        out = []
        for r in self._result(exp_id).runs:
            if not r.measurements:
                continue
            laps: dict[int, dict] = {}
            for m in r.measurements:
                if "lap" not in m.metadata:
                    continue
                lap = laps.setdefault(m.metadata["lap"], {"lap": m.metadata["lap"], "flags": {}})
                lap[m.metric] = round(float(m.value), 4) if isinstance(m.value, (int, float)) else m.value
                notable = [NOTABLE.get(f, f) for f in m.quality_flags if f not in ("run_id_bound", "timestamp_assumed_utc", "timestamp_unavailable")]
                if notable:
                    lap["flags"][m.metric] = notable
            rows = [laps[k] for k in sorted(laps)]
            for lap in rows:
                lap["outlier"] = "outlier" in " ".join(
                    lap["flags"].get("lap_time", [])
                )  # lap flagged by its lap time; sector flags stay as tags
            out.append({"run_id": r.run_id, "order": r.order, "status": r.status.value, "configuration": r.configuration,
                        "source": r.measurements[0].source, "synthetic": not any(m.metadata.get("imported") for m in r.measurements),
                        "laps": rows})  # fmt: skip
        return out

    # ------------------------------------------------------------------ analysis & optimisation
    def _completed(self, exp_id: str) -> ExperimentResult:
        res = self._result(exp_id)
        if not any(r.status == RunStatus.COMPLETED for r in res.runs):
            raise Conflict("no completed runs yet: execute the experiment first")
        return res

    async def analyse(self, exp_id: str, alpha: float = 0.05, bootstrap: bool = False) -> dict:
        res = self._completed(exp_id)
        analysis = await asyncio.to_thread(self.svc.analyse, res, alpha=alpha, bootstrap=bootstrap)
        fx = narrative.factor_effects(analysis, alpha)
        rec = narrative.recommendation(res, analysis, alpha)
        rows = {r["run_id"]: r for r in self.runs(exp_id)}
        return {
            "alpha": alpha, "summary": self._summary(res), "effects": fx, "recommendation": rec,
            "main_effects": [e.model_dump(mode="json") for e in analysis.main_effects if e.metric == "lap_time"],
            "comparisons": [e.model_dump(mode="json") for e in analysis.effects if e.metric == "lap_time"][:60],
            "interaction_tests": [i.model_dump(mode="json") for i in analysis.interactions if i.metric == "lap_time"],
            "descriptive": analysis.descriptive, "sample_sizes": analysis.sample_sizes, "warnings": analysis.warnings,
            "limitations": analysis.limitations, "assumptions": analysis.assumptions,
            "feasible_run_ids": analysis.feasible_run_ids, "pareto_run_ids": analysis.pareto_run_ids,
            "infeasible": [{"run_id": i["run_id"], "violations": i["violations"], "lap_time": i["objectives"].get("lap_time")} for i in analysis.infeasible_runs],
            "best_run_id": analysis.best_run_id, "runs": rows, "excluded_runs": analysis.excluded_runs, "outlier_runs": analysis.outlier_flags,
            "parameters": analysis.parameters,
        }  # fmt: skip

    async def optimise_suggest(self, exp_id: str, count: int, method: str) -> list[dict]:
        res = self._completed(exp_id)
        if len(res.runs) >= res.spec.run_budget:
            raise Conflict("run budget exhausted")
        recs = await asyncio.to_thread(self.svc.optimise, res.spec, res, count=count, method=method)
        if not recs:
            raise Conflict("the optimiser found no unseen feasible candidate")
        return [r.model_dump(mode="json") for r in recs]

    async def optimise_apply(self, exp_id: str, count: int, method: str) -> dict:
        res = self._completed(exp_id)
        if any(r.status in (RunStatus.QUEUED, RunStatus.RUNNING) for r in res.runs):
            raise Conflict("wait for the current runs to finish before requesting new ones")
        recs = await self.optimise_suggest(exp_id, count, method)
        iteration = len(self.repo.get_plan(exp_id).design_info.get("sequential_extensions", [])) + 1
        runs = self.svc.add_runs(exp_id, [r["configuration"] for r in recs], iteration=iteration, source=f"optimizer:{method}")
        self._register_adapter(res.spec)
        self.runner.submit(exp_id)
        return {"iteration": iteration, "added_runs": [r.run_id for r in runs], "recommendations": recs}

    def optimisation_history(self, exp_id: str) -> list[dict]:
        self._result(exp_id)
        return [r.model_dump(mode="json") for r in self.repo.list_recommendations(exp_id)]

    async def recommendation(self, exp_id: str) -> dict:
        res = self._completed(exp_id)
        analysis = await asyncio.to_thread(self.svc.analysis_engine.analyse, res)
        return narrative.recommendation(res, analysis)

    # ------------------------------------------------------------------ reports
    async def report(self, exp_id: str) -> dict:
        res = self._completed(exp_id)
        analysis = await asyncio.to_thread(self.svc.analyse, res)
        report = await asyncio.to_thread(self.svc.report, res, analysis)
        rec = narrative.recommendation(res, analysis)
        return {
            "report": report.model_dump(mode="json"),
            "recommendation": rec,
            "effects": rec["effects"],
            "synthetic_notice": notice_for(mode_of(res.spec)),
            "notice": provenance_notice(self._provenance(res)),
            "source_label": self._provenance(res),
        }

    async def report_html(self, exp_id: str) -> str:
        data = await self.report(exp_id)
        res = self._result(exp_id)
        runs = self.runs(exp_id)
        design = self.design_view(res)
        return report_html.render(res.spec.name, res.spec.hypothesis, data["recommendation"], design, runs, data["effects"], data["report"], mode_of(res.spec),
                                  provenance=self._provenance(res))

    def csv_export(self, exp_id: str, kind: str) -> str:
        res = self._result(exp_id)
        if kind == "runs":
            by_run = {r.run_id: (LABEL_IMPORTED if _run_imported(r) else LABEL_SYNTHETIC) for r in res.runs}
            return self._with_source(ReportEngine.to_csv(res), lambda row: by_run.get(row.get("run_id"), LABEL_SYNTHETIC))
        if kind == "measurements":
            return ReportEngine.measurements_csv(res)  # already has a per-measurement ``source`` column
        if kind == "effects":
            label = self._provenance(self._completed(exp_id))
            return self._with_source(ReportEngine.effects_csv(self.svc.analysis_engine.analyse(self._completed(exp_id))), lambda row: label)
        raise NotFound(kind)

    @staticmethod
    def _with_source(text: str, label_of) -> str:
        """Append a ``source`` column (the provenance label of the row) to a CSV produced by the SDK."""
        rows = list(csv.DictReader(io.StringIO(text)))
        header = list(rows[0].keys()) if rows else next(csv.reader(io.StringIO(text)), [])
        out = io.StringIO()
        w = csv.DictWriter(out, fieldnames=[*header, "source"], lineterminator="\r\n")
        w.writeheader()
        for row in rows:
            w.writerow({**row, "source": label_of(row)})
        return out.getvalue()

    def csv_notice(self, exp_id: str) -> str:
        """The data-source notice for a CSV download (sent as a response header, so the CSV itself stays machine-readable)."""
        return provenance_notice(self._provenance(self._result(exp_id)))

    # ------------------------------------------------------------------ Setup_Link (imported session -> experiment)
    @staticmethod
    def _finite(v: Any) -> float | None:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if f == f and f not in (float("inf"), float("-inf")) else None

    def session_lap_values(self, hist, session_id: str, driver: str) -> dict[str, dict[int, float]]:
        """``{metric: {lap_number: value}}`` the session holds for one driver. Metrics without any value are absent."""
        out: dict[str, dict[int, float]] = {}
        for rec in hist.laps(session_id, driver):
            lap = rec.get("lap_number")
            if lap is None or self._finite(lap) is None:
                continue
            for metric, col in SETUP_LINK_LAP_METRICS.items():
                v = self._finite(rec.get(col))
                if v is not None and v > 0:
                    out.setdefault(metric, {})[int(lap)] = v
        tel = hist.repo.telemetry(session_id, hist._driver_code(session_id, driver))
        if not tel.empty and {"lap_number", "speed_kph"} <= set(tel.columns):
            top = tel.dropna(subset=["lap_number", "speed_kph"]).groupby("lap_number")["speed_kph"].max()
            for lap, v in top.items():
                if self._finite(v) is not None and v >= 0:
                    out.setdefault("top_speed", {})[int(lap)] = float(v)
        return out

    def setup_link_options(self, hist, session_id: str, driver: str, laps: list[int] | None = None) -> dict:
        """Metrics the Setup_Link can offer: ``available`` False (not selectable) when the session has no value for the driver and laps."""
        values = self.session_lap_values(hist, session_id, driver)
        wanted = set(laps) if laps else None
        metrics = [{"metric": m, "unit": METRIC_UNITS[m],
                    "available": any(wanted is None or lap in wanted for lap in values.get(m, {}))} for m in SETUP_LINK_METRICS]
        return {"driver": driver, "laps": sorted({lap for v in values.values() for lap in v}), "metrics": metrics}

    @staticmethod
    def _check_mapping(spec: ExperimentSpec, mapping: Any) -> tuple[dict[str, Any], bool]:
        """Validate the Setup_Configuration against the experiment's factors. Returns (configuration, verified)."""
        names = [f.name for f in spec.factors]
        mapping = mapping if isinstance(mapping, dict) else {}
        missing, config, verified = [], {}, True
        for f in spec.factors:
            entry = mapping.get(f.name)
            entry = entry if isinstance(entry, dict) else {}
            value, flag = entry.get("value"), entry.get("verified")
            if value is None or (isinstance(value, str) and not value.strip()) or not isinstance(flag, bool):
                missing.append(f.name)
                continue
            if f.factor_type == "continuous":
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    raise SetupLinkError(f"factor '{f.name}' needs a numeric value, got {value!r}") from None
            config[f.name] = value
            verified = verified and flag
        if missing:
            raise SetupLinkError("the Setup_Configuration is unknown or incomplete; missing a value or a verified/unverified statement for factor(s): "
                                 + ", ".join(missing), missing)
        unknown = sorted(set(mapping) - set(names))
        if unknown:
            raise SetupLinkError(f"unknown factor(s) for this experiment: {', '.join(unknown)}")
        return config, verified

    def _purge_run(self, exp_id: str, run_id: str, plan, status) -> None:
        """Remove everything one failed Setup_Link import wrote (run, measurements, queue rows) and restore the plan and status."""
        engine = self.repo.db_engine
        with engine.begin() as c:
            for table in ("measurements", "processed_measurements", "jobs", "state_log", "runs"):
                c.execute(text(f"DELETE FROM {table} WHERE run_id = :r"), {"r": run_id})
        self.repo.save_plan(plan)
        self.repo.set_status(exp_id, status, "setup link rolled back")

    async def setup_link(self, hist, session_id: str, req: dict) -> dict:
        """Send selected imported measurements to a TRIALFORGE experiment (Req 15.2-15.8, 15.10).

        ``req``: ``driver``, ``laps`` (1-100), ``metrics`` (1-50), ``experiment_id`` or ``new_experiment`` (an ExperimentRequest body),
        and ``factors`` = ``{factor: {"value": v, "verified": bool}}``. Everything is validated before anything is written.
        One run is added to the experiment holding one measurement per selected lap and metric, each with the session's
        Provenance_Status and ``source_session_id``. An unverified mapping flags them ``unverified_setup`` (excluded from analysis and training).
        Any failure after validation removes the run and its measurements again."""
        driver = str(req.get("driver") or "").strip()
        laps_in, metrics_in = req.get("laps"), req.get("metrics")
        if not driver or not isinstance(req.get("driver"), str):
            raise SetupLinkError("select exactly one driver")
        if not isinstance(laps_in, list) or not 1 <= len(set(laps_in)) <= SETUP_LINK_MAX_LAPS:
            raise SetupLinkError(f"select between 1 and {SETUP_LINK_MAX_LAPS} laps")
        if not isinstance(metrics_in, list) or not 1 <= len(set(metrics_in)) <= SETUP_LINK_MAX_METRICS:
            raise SetupLinkError(f"select between 1 and {SETUP_LINK_MAX_METRICS} metrics")
        if not all(isinstance(x, int) and not isinstance(x, bool) for x in laps_in):
            raise SetupLinkError("laps must be whole lap numbers")
        laps, metrics = sorted(set(laps_in)), list(dict.fromkeys(metrics_in))
        exp_id, new = req.get("experiment_id"), req.get("new_experiment")
        if bool(exp_id) == bool(new):
            raise SetupLinkError("select exactly one target experiment: an existing experiment_id or a new_experiment")
        session = hist.get_session(session_id)
        values = self.session_lap_values(hist, session_id, driver)
        absent = [lap for lap in laps if not any(lap in v for v in values.values())]
        if absent:
            raise SetupLinkError(f"laps not found for driver {driver} in this session: {', '.join(map(str, absent))}")
        unavailable = [m for m in metrics if m not in SETUP_LINK_METRICS or not any(lap in values.get(m, {}) for lap in laps)]
        if unavailable:
            raise SetupLinkError(f"metric(s) unavailable for driver {driver} and the selected laps: {', '.join(map(str, unavailable))}")
        if exp_id:
            spec = self._result(exp_id).spec if exp_id in self.repo.list_ids() else None
            if spec is None:
                raise NotFound(exp_id)
        else:
            spec = self.build_spec(ExperimentRequest(**new), new.get("experiment_id") or "f1-" + uuid.uuid4().hex[:8])
        config, verified = self._check_mapping(spec, req.get("factors"))
        if not exp_id:
            exp_id = self.create(ExperimentRequest(**new))["id"]
            spec = self._result(exp_id).spec
        status_before, plan_before = self.repo.get_status(exp_id), self.repo.get_plan(exp_id).model_copy(deep=True)
        provenance = session.get("provenance_status")
        synthetic = bool(session.get("synthetic")) or provenance == PROVENANCE_DEMO
        flags = [] if verified else [UNVERIFIED_FLAG]
        run = None
        try:
            run = self.svc.add_runs(exp_id, [config], iteration=len(plan_before.design_info.get("sequential_extensions", [])) + 1,
                                    source="setup_link")[0]
            ms = []
            for lap in laps:
                for metric in metrics:
                    if lap not in values.get(metric, {}):
                        continue
                    meta = {"synthetic": synthetic, "imported": not synthetic, "provenance_status": provenance, "lap": lap,
                            "source_session_id": session_id, "driver": driver, "setup_verified": verified, "timestamp_unavailable": True}
                    ms.append(Measurement(run_id=run.run_id, metric=metric, value=values[metric][lap], unit=METRIC_UNITS[metric],
                                          timestamp=UNAVAILABLE_TIMESTAMP, source=f"setup_link:{session_id}", metadata=meta, channel=f"lap_{lap}",
                                          quality_flags=[*flags, "timestamp_unavailable"]))
            strict, self.svc.strict_required_metrics = self.svc.strict_required_metrics, False  # the user chooses which metrics to send
            try:
                done = await self.svc.ingest_measurements(exp_id, run.run_id, ms)
            finally:
                self.svc.strict_required_metrics = strict
            if not verified and done.status == RunStatus.FAILED:  # every measurement is excluded, so the SDK has no valid data to complete the run
                done.error = "setup mapping is unverified: measurements are kept but excluded from analysis and training"
                self.repo.save_run(done)
            stored = self.repo.get_run(run.run_id)
            if len(stored.measurements) != len(ms):
                raise RuntimeError("stored measurement count differs from the import")
        except Exception:
            if run is not None:
                self._purge_run(exp_id, run.run_id, plan_before, status_before)
            raise
        return {"experiment_id": exp_id, "run_id": run.run_id, "measurements_created": len(ms), "verified": verified,
                "excluded_from_training": not verified, "provenance_status": provenance, "source_session_id": session_id}

    def linked_experiments(self, session_id: str) -> list[dict]:
        """Experiments holding measurements imported from ``session_id`` (installed as the historical service's link lookup)."""
        import json

        with self.repo.db_engine.connect() as c:
            rows = c.execute(text("SELECT DISTINCT experiment_id, payload_json FROM measurements WHERE payload_json LIKE :p"),
                             {"p": f"%{session_id}%"}).all()
        ids = sorted({r[0] for r in rows if json.loads(r[1]).get("metadata", {}).get("source_session_id") == session_id})
        out = []
        for i in ids:
            try:
                out.append({"experiment_id": i, "name": self.repo.get_spec(i).name})
            except KeyError:
                continue
        return out

    def delete_supported(self) -> bool:
        return False

    @staticmethod
    def is_active(status: str) -> bool:
        return status in (ExperimentStatus.RUNNING.value, ExperimentStatus.QUEUED.value)
