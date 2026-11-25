"""LabService: the F1 workflow on top of the TRIALFORGE SDK.

The SDK does all experiment work (design, execution, measurements, statistics, optimisation, persistence, reports);
this class only maps the application's concepts (setup factors, stability limit, telemetry) onto SDK objects.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

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
    MeasurementDefinition,
    MeasurementEngine,
    Objective,
    ReportEngine,
    RunStatus,
)

from db.config import resolve_db_url, sqlite_file_path
from db.models.common import APP_F1
from db.scoped import ScopedExperimentRepository
from f1lab import notice_for
from f1lab.adapters.sim_adapter import F1SimulatorAdapter
from f1lab.adapters.telemetry import parse_telemetry_csv, to_measurements
from f1lab.adapters.vehicle_model import BASELINE, FACTORS, METRIC_UNITS
from f1lab.api.schemas import ExperimentRequest
from f1lab.services import narrative, report_html

ADAPTER_PREFIX = "f1-sim/"
GENERATORS = {4: ["D=ABC"], 3: ["C=AB"]}  # half fractions of the enabled factors (resolution IV and III)
NOTABLE = {"unit_converted": "unit converted", "statistical_outlier": "outlier", "sensor_warning": "sensor warning", "import_time_assumed": "time = import time"}


def mode_of(spec: ExperimentSpec) -> str:
    return spec.metadata.get("mode", "synthetic")


class NotFound(KeyError):
    pass


class Conflict(RuntimeError):
    pass


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
            metadata={"app": "f1-setup-lab", "synthetic": True, "lab": req.simulator.model_dump(), "min_stability": req.min_stability},
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
        imported = any(m.metadata.get("imported") for m in r.measurements)
        return {
            "run_id": r.run_id, "order": r.order, "status": r.status.value, "configuration": r.configuration,
            "coded": r.coded, "replicate": r.replicate, "attempts": r.attempts, "error": r.error,
            "outcomes": {k: round(v, 4) for k, v in out.items()},
            "feasible": r.metadata.get("feasible") if r.status == RunStatus.COMPLETED else None,
            "point_kind": r.metadata.get("point_kind") or ("center" if r.metadata.get("center_point") else None),
            "iteration": r.metadata.get("sequential_iteration"), "imported": imported,
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

    def detail(self, exp_id: str) -> dict:
        res = self._result(exp_id)
        d = self._summary(res)
        d["runs"] = [self._run_row(r) for r in res.runs]
        d["design"] = self.design_view(res)
        d["simulator"] = res.spec.metadata.get("lab", {})
        d["synthetic_notice"] = notice_for(mode_of(res.spec))
        d["notice"] = d["synthetic_notice"]
        d["source_label"] = "SYNTHETIC"
        return d

    def design_view(self, res: ExperimentResult) -> dict:
        info = res.plan.design_info
        keep = ("type", "k", "p", "runs", "generators", "defining_relation", "resolution", "alias_structure", "factor_letters",
                "replicates", "center_points", "complete", "limitations", "alpha", "inscribed", "full_grid_size", "base_factors")  # fmt: skip
        return {
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
    def telemetry_preview(self, csv_text: str, time_unit: str = "s") -> dict:
        """Parse a CSV and use the SDK's MeasurementEngine to convert units and flag outlier laps (nothing is deleted)."""
        parsed = parse_telemetry_csv(csv_text, time_unit)
        out: dict[str, Any] = {"errors": parsed.errors, "laps": [], "summary": None, "time_unit": time_unit}
        if not parsed.laps:
            return out
        ms = to_measurements("preview", parsed, "preview")
        eng = MeasurementEngine(measurement_definitions())
        _, assessed, warnings = eng.assess(ms, "preview")
        by_lap: dict[int, dict] = {}
        for m in assessed:
            lap = by_lap.setdefault(m.metadata["lap"], {"lap": m.metadata["lap"], "flags": {}})
            lap[m.metric] = round(float(m.value), 4)
            notable = [NOTABLE.get(f, f) for f in m.quality_flags if f not in ("run_id_bound", "timestamp_assumed_utc", "import_time_assumed")]
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

    async def attach_telemetry(self, exp_id: str, run_id: str, csv_text: str, time_unit: str, filename: str) -> dict:
        res = self._result(exp_id)
        run = next((r for r in res.runs if r.run_id == run_id), None)
        if run is None:
            raise NotFound(run_id)
        parsed = parse_telemetry_csv(csv_text, time_unit)
        if not parsed.laps:
            raise ValueError("; ".join(parsed.errors) or "no usable laps in the file")
        updated = await self.svc.ingest_measurements(exp_id, run_id, to_measurements(run_id, parsed, f"csv:{filename}"))
        return {"run": self._run_row(updated), "row_errors": parsed.errors}

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
                notable = [NOTABLE.get(f, f) for f in m.quality_flags if f not in ("run_id_bound", "timestamp_assumed_utc", "import_time_assumed")]
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
        }

    async def report_html(self, exp_id: str) -> str:
        data = await self.report(exp_id)
        res = self._result(exp_id)
        runs = self.runs(exp_id)
        design = self.design_view(res)
        return report_html.render(res.spec.name, res.spec.hypothesis, data["recommendation"], design, runs, data["effects"], data["report"], mode_of(res.spec))

    def csv_export(self, exp_id: str, kind: str) -> str:
        res = self._result(exp_id)
        if kind == "runs":
            return ReportEngine.to_csv(res)
        if kind == "measurements":
            return ReportEngine.measurements_csv(res)
        if kind == "effects":
            return ReportEngine.effects_csv(self.svc.analysis_engine.analyse(self._completed(exp_id)))
        raise NotFound(kind)

    def delete_supported(self) -> bool:
        return False

    @staticmethod
    def is_active(status: str) -> bool:
        return status in (ExperimentStatus.RUNNING.value, ExperimentStatus.QUEUED.value)
