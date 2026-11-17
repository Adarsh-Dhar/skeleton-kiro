"""LabService: the WATTWISE workflow on top of the TRIALFORGE SDK.

The SDK does all experiment work; this class maps electricity concepts (tariffs, EV departure charge, appliance schedules, meter CSVs)
onto SDK objects.
"""

from __future__ import annotations

import asyncio
import statistics
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
    Factor,
    LocalBackgroundRunner,
    Measurement,
    MeasurementDefinition,
    MeasurementEngine,
    Objective,
    ReportEngine,
    RunStatus,
    SQLiteExperimentRepository,
)

from wattwise import SIMULATED_NOTICE
from wattwise.adapters.household_model import BASELINE, FACTORS, METRIC_UNITS, clock, simulate, summarise
from wattwise.adapters.meter_adapter import MeterSimulatorAdapter
from wattwise.adapters.meter_csv import parse_meter_csv, to_series
from wattwise.api.schemas import DayRequest, ExperimentRequest, MeterAttachRequest, MeterRequest
from wattwise.services import narrative, report_html
from wattwise.services.tariffs import TariffError, TariffStore

ADAPTER_PREFIX = "wattwise-sim/"
GENERATORS = {3: ["C=AB"]}
NOTABLE = {"unit_converted": "unit converted", "statistical_outlier": "outlier", "sensor_warning": "sensor warning", "duplicate": "duplicate", "out_of_order": "out of order"}


class NotFound(KeyError):
    pass


class Conflict(RuntimeError):
    pass


def measurement_definitions() -> list[MeasurementDefinition]:
    defs = [MeasurementDefinition(metric=m, unit=u, required=m in ("energy_cost", "energy_kwh", "peak_power_kw", "departure_charge_pct"))
            for m, u in METRIC_UNITS.items() if m != "grid_power"]  # fmt: skip
    defs.append(MeasurementDefinition(metric="grid_power", unit="kW", time_series=True, sampling_interval_seconds=900, required=False, outlier_method="none"))
    return defs


class LabService:
    def __init__(self, db_path: str, tariff_path: str | None = None) -> None:
        self.registry = AdapterRegistry()
        url = f"sqlite:///{db_path}" if "://" not in db_path else db_path
        self.repo = SQLiteExperimentRepository(url)
        self.svc = ExperimentService(registry=self.registry, repository=self.repo)
        self.runner = LocalBackgroundRunner(self.svc)
        base = Path(db_path).parent if "://" not in db_path else Path("data")
        self.tariffs = TariffStore(tariff_path or base / "tariffs.json")

    # ------------------------------------------------------------------ lifecycle
    async def startup(self) -> list[dict]:
        for exp_id in self.repo.list_ids():
            self._register_adapter(self.repo.get_spec(exp_id))
        return await self.svc.recover()

    def _register_adapter(self, spec: ExperimentSpec) -> None:
        if spec.adapter_name and spec.adapter_name not in self.registry:
            lab = spec.metadata.get("lab", {})
            self.registry.register(MeterSimulatorAdapter(spec.adapter_name, lab["household"], latency_seconds=lab.get("latency_seconds", 0.0),
                                                         fault_rate=lab.get("fault_rate", 0.0)))  # fmt: skip

    async def shutdown(self) -> None:
        await self.runner.shutdown()
        await self.registry.shutdown_all()

    # ------------------------------------------------------------------ tariffs & household
    def resolve_tariff(self, tariff_id: str | None, inline: Any) -> dict[str, Any]:
        if inline is not None:
            data = inline.model_dump()
            from wattwise.adapters.household_model import validate_tariff

            issues = validate_tariff(data)
            if issues:
                raise TariffError(issues)
            return {"id": "inline", **data}
        try:
            return self.tariffs.get(tariff_id or "time-of-use")
        except KeyError:
            raise NotFound(f"tariff {tariff_id}") from None

    def household(self, req: Any) -> dict[str, Any]:
        return {"ev": req.ev.model_dump(), "tariff": self.resolve_tariff(req.tariff_id, req.tariff), "base_load_kw": req.base_load_kw,
                "appliances": [a.model_dump() for a in req.appliances]}  # fmt: skip

    # ------------------------------------------------------------------ creation
    def build_spec(self, req: ExperimentRequest, exp_id: str, household: dict[str, Any]) -> ExperimentSpec:
        factors = []
        for name, s in req.factors.items():
            if s.enabled:
                base = BASELINE[name]
                factors.append(Factor(name=name, factor_type="continuous", lower=s.low, upper=s.high, unit=FACTORS[name]["unit"],
                                      default=base if s.low <= base <= s.high else round((s.low + s.high) / 2, 6)))  # fmt: skip
        k, d = len(factors), req.design
        method = {"full_factorial": "factorial"}.get(d.method, d.method)
        opts: dict[str, Any] = {"replicates": d.replicates}
        if method == "factorial":
            opts.update(continuous_levels=d.levels, center_points=d.center_points)
        elif method == "ofat":
            opts.update(continuous_levels=d.levels)
        elif method == "fractional_factorial":
            opts["center_points"] = d.center_points
            if k in GENERATORS:
                opts["generators"] = GENERATORS[k]
        elif method in ("central_composite", "box_behnken"):
            opts = {"center_points": max(d.center_points, 1)}
        elif method == "random":
            opts = {"n_runs": min(d.n_runs, d.run_budget)}
        objectives = [Objective(metric="energy_cost", direction="minimize")]
        if req.also_minimise_peak:
            objectives.append(Objective(metric="peak_power_kw", direction="minimize", weight=0.5))
        return ExperimentSpec(
            experiment_id=exp_id, name=req.name, hypothesis=req.hypothesis, description="WATTWISE LAB experiment on a SIMULATED household.",
            factors=factors, objectives=objectives,
            constraints=[Constraint(metric="departure_charge_pct", kind="output", minimum=req.ev.desired_charge_pct)],
            measurement_definitions=measurement_definitions(), design_method=method, design_options=opts,
            adapter_name=ADAPTER_PREFIX + exp_id, run_budget=d.run_budget, random_seed=d.seed,
            execution=ExecutionPolicy(max_retries=req.simulator.max_retries, max_concurrency=4, run_timeout_seconds=120),
            metadata={"app": "wattwise-lab", "simulated": True, "desired_charge_pct": req.ev.desired_charge_pct,
                      "lab": {"household": household, **req.simulator.model_dump()}},
        )  # fmt: skip

    def create(self, req: ExperimentRequest) -> dict:
        exp_id = req.experiment_id or "ww-" + uuid.uuid4().hex[:8]
        if self.repo.has_experiment(exp_id):
            raise Conflict(f"an experiment with id '{exp_id}' already exists")
        household = self.household(req)
        spec = self.build_spec(req, exp_id, household)
        self._register_adapter(spec)
        try:
            self.svc.design(spec)
        except Exception:
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
    def _hh(res: ExperimentResult) -> dict[str, Any]:
        return res.spec.metadata["lab"]["household"]

    @staticmethod
    def _run_row(r) -> dict:
        out = narrative.run_outcomes(r) if r.status == RunStatus.COMPLETED else {}
        return {
            "run_id": r.run_id, "order": r.order, "status": r.status.value, "configuration": r.configuration,
            "schedule": narrative.schedule_text(r.configuration), "coded": r.coded, "replicate": r.replicate, "attempts": r.attempts, "error": r.error,
            "outcomes": {k: round(v, 4) for k, v in out.items()}, "feasible": r.metadata.get("feasible") if r.status == RunStatus.COMPLETED else None,
            "point_kind": r.metadata.get("point_kind") or ("center" if r.metadata.get("center_point") else None),
            "iteration": r.metadata.get("sequential_iteration"), "imported": any(m.metadata.get("reading_kind") == "imported" for m in r.measurements),
        }  # fmt: skip

    def _summary(self, res: ExperimentResult) -> dict:
        counts = {s.value: 0 for s in RunStatus}
        for r in res.runs:
            counts[r.status.value] += 1
        rows = [self._run_row(r) for r in res.runs]
        feas = [r for r in rows if r["status"] == "completed" and r["feasible"] and "energy_cost" in r["outcomes"]]
        best = min(feas, key=lambda r: r["outcomes"]["energy_cost"]) if feas else None
        spec, hh = res.spec, self._hh(res)
        base = simulate(BASELINE, hh)["metrics"]
        kwh = sum(r["outcomes"].get("energy_kwh", 0.0) for r in rows if r["status"] == "completed")
        return {
            "id": spec.experiment_id, "name": spec.name, "hypothesis": spec.hypothesis, "status": res.status.value, "created_at": spec.created_at.isoformat(),
            "design_method": res.plan.method, "run_budget": spec.run_budget, "runs_total": len(res.runs), "counts": counts,
            "desired_charge_pct": spec.metadata.get("desired_charge_pct"), "tariff": hh["tariff"]["name"], "simulated_kwh": round(kwh, 1),
            "factors": [{"name": f.name, "low": f.lower, "high": f.upper, "unit": f.unit, "label": FACTORS[f.name]["label"], "clock": FACTORS[f.name]["clock"]} for f in spec.factors],
            "baseline_cost": round(base["energy_cost"], 3),
            "best": {"run_id": best["run_id"], "cost": best["outcomes"]["energy_cost"], "kwh": best["outcomes"].get("energy_kwh"), "peak": best["outcomes"].get("peak_power_kw"),
                     "charge": best["outcomes"].get("departure_charge_pct"), "schedule": best["schedule"], "configuration": best["configuration"],
                     "saving_pct": round(100 * (base["energy_cost"] - best["outcomes"]["energy_cost"]) / base["energy_cost"], 1)} if best else None,
            "multi_objective": len(spec.objectives) > 1,
        }  # fmt: skip

    def list(self) -> list[dict]:
        out = []
        for i in self.repo.list_ids():
            try:
                out.append(self._summary(self._result(i)))
            except NotFound:
                continue
        return sorted(out, key=lambda s: s["created_at"], reverse=True)

    def detail(self, exp_id: str) -> dict:
        res = self._result(exp_id)
        d = self._summary(res)
        d["runs"] = [self._run_row(r) for r in res.runs]
        info = res.plan.design_info
        keep = ("type", "k", "p", "runs", "generators", "defining_relation", "resolution", "factor_letters", "replicates", "center_points", "complete", "limitations", "levels", "full_grid_size")
        d["design"] = {"method": res.plan.method, "seed": res.plan.random_seed, "runs_planned": len(res.plan.runs), "randomization": res.plan.randomization.get("scheme"),
                       "control": res.plan.control_configuration, **{k: info[k] for k in keep if k in info}}  # fmt: skip
        d["household"] = self._hh(res)
        d["simulator"] = {k: v for k, v in res.spec.metadata["lab"].items() if k != "household"}
        d["simulated_notice"] = SIMULATED_NOTICE
        return d

    def runs(self, exp_id: str) -> list[dict]:
        return [self._run_row(r) for r in self._result(exp_id).runs]

    # ------------------------------------------------------------------ simulation previews (no SDK run involved)
    @staticmethod
    def _profile(sim: dict) -> dict:
        st = sim["steps"]
        m = sim["metrics"]
        return {"steps": [{"t": s["t"], "base": round(s["base"], 3), "appliances": round(s["appliances"], 3), "ev": round(s["ev"], 3), "kw": round(s["kw"], 3), "price": s["price"]} for s in st],
                "metrics": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in m.items() if k != "cost_by_period"},
                "cost_by_period": {k: {a: round(b, 4) for a, b in v.items()} for k, v in m["cost_by_period"].items()}}  # fmt: skip

    def day(self, req: DayRequest) -> dict:
        hh = {"ev": req.ev.model_dump(), "tariff": self.resolve_tariff(req.tariff_id, req.tariff), "base_load_kw": req.base_load_kw, "appliances": [a.model_dump() for a in req.appliances]}
        for k, v in req.setup.items():
            if k not in FACTORS or not (FACTORS[k]["low"] <= v <= FACTORS[k]["high"]):
                raise ValueError(f"setup.{k} is not a valid schedule parameter value")
        return {**self._profile(simulate(req.setup, hh)), "tariff": hh["tariff"], "setup": {**BASELINE, **req.setup}, "simulated": True}

    def run_profile(self, exp_id: str, run_id: str) -> dict:
        res = self._result(exp_id)
        run = next((r for r in res.runs if r.run_id == run_id), None)
        if run is None:
            raise NotFound(run_id)
        hh = self._hh(res)
        return {**self._profile(simulate(run.configuration, hh)), "tariff": hh["tariff"], "setup": {**BASELINE, **run.configuration}, "run_id": run_id, "model_profile": True}

    def overview(self) -> dict:
        exps = self.list()
        best = [e["best"]["saving_pct"] for e in exps if e["best"]]
        default_hh = {"ev": {"battery_kwh": 60.0, "initial_soc_pct": 30.0, "desired_charge_pct": 80.0, "departure_hour": 7.5, "efficiency": 0.92},
                      "tariff": self.tariffs.get("time-of-use") if any(t["id"] == "time-of-use" for t in self.tariffs.list()) else self.tariffs.list()[0],
                      "base_load_kw": 0.35, "appliances": [{"name": "Dishwasher", "power_kw": 1.1, "duration_h": 2.0, "flexible": True, "fixed_start": 20.0},
                                                          {"name": "Washing machine", "power_kw": 0.5, "duration_h": 1.5, "flexible": True, "fixed_start": 19.0},
                                                          {"name": "Tumble dryer", "power_kw": 2.0, "duration_h": 1.5, "flexible": False, "fixed_start": 18.0}]}  # fmt: skip
        return {"experiments": exps, "kpis": {"experiments": len(exps), "runs": sum(e["counts"]["completed"] for e in exps), "best_saving_pct": max(best) if best else None,
                                              "simulated_kwh": round(sum(e["simulated_kwh"] for e in exps), 1)},
                "baseline_day": {**self._profile(simulate(BASELINE, default_hh)), "tariff": default_hh["tariff"], "setup": dict(BASELINE)}, "notice": SIMULATED_NOTICE}  # fmt: skip

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
        return self._summary(await self.svc.cancel(exp_id))

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

    # ------------------------------------------------------------------ smart-meter CSV
    def _meter_read(self, csv_text: str, unit: str) -> tuple[Any, list[Measurement], list[str], dict[str, int]]:
        parsed = parse_meter_csv(csv_text, unit)
        if not parsed.readings:
            raise ValueError("; ".join(parsed.errors) or "no usable readings")
        ms = to_series("preview", parsed, "csv")
        ts = sorted(m.timestamp for m in ms)
        deltas = [(b - a).total_seconds() for a, b in zip(ts, ts[1:]) if (b - a).total_seconds() > 0]
        interval = statistics.median(deltas) if deltas else 900.0
        if interval > 3600:
            raise ValueError(f"readings are {interval / 60:.0f} minutes apart; the maximum supported interval is 60 minutes")
        eng = MeasurementEngine([MeasurementDefinition(metric="grid_power", unit="kW", time_series=True, sampling_interval_seconds=interval, outlier_method="none")])
        valid, assessed, warnings = eng.assess(ms, "preview")
        quality = {"duplicates": sum("duplicate" in m.quality_flags for m in assessed), "out_of_order": sum("out_of_order" in m.quality_flags for m in assessed),
                   "unit_converted": sum("unit_converted" in m.quality_flags for m in assessed), "invalid": len(assessed) - len(valid)}  # fmt: skip
        return parsed, sorted(valid, key=lambda m: m.timestamp), warnings + parsed.errors, quality | {"interval_s": interval}

    def _meter_summary(self, valid: list[Measurement], interval_s: float, tariff: dict[str, Any]) -> dict[str, Any]:
        readings = [(m.timestamp.hour + m.timestamp.minute / 60 + m.timestamp.second / 3600, float(m.value)) for m in valid]  # kW (SDK converted W)
        s = summarise(readings, tariff, step_h=interval_s / 3600)
        hourly: dict[int, list[float]] = {}
        for t, kw in readings:
            hourly.setdefault(int(t) % 24, []).append(kw)
        return {"energy_kwh": s["energy_kwh"], "energy_cost": s["energy_cost"], "energy_charge": s["energy_charge"], "demand_charge": s["demand_charge"],
                "peak_power_kw": s["peak_power_kw"], "average_kw": sum(k for _, k in readings) / len(readings), "readings": len(readings),
                "hours_covered": len(readings) * interval_s / 3600, "interval_minutes": interval_s / 60,
                "cost_by_period": {k: {a: round(b, 4) for a, b in v.items()} for k, v in s["cost_by_period"].items()},
                "hourly_profile": [{"hour": h, "kw": round(sum(v) / len(v), 3)} for h, v in sorted(hourly.items())]}  # fmt: skip

    def meter_preview(self, req: MeterRequest) -> dict:
        parsed, valid, warnings, quality = self._meter_read(req.csv, req.unit)
        tariff = self.resolve_tariff(req.tariff_id, req.tariff)
        step = max(1, len(valid) // 480)
        return {"summary": self._meter_summary(valid, quality["interval_s"], tariff), "quality": quality, "warnings": warnings, "errors": parsed.errors, "tariff": tariff,
                "unit": parsed.unit, "series": [{"t": m.timestamp.isoformat(), "kw": round(float(m.value), 4)} for m in valid[::step]], "reading_kind": "imported"}  # fmt: skip

    async def attach_meter(self, exp_id: str, run_id: str, req: MeterAttachRequest) -> dict:
        res = self._result(exp_id)
        if not any(r.run_id == run_id for r in res.runs):
            raise NotFound(run_id)
        parsed, valid, warnings, quality = self._meter_read(req.csv, req.unit)
        tariff = self._hh(res)["tariff"]  # the experiment's own tariff, so imported and simulated runs are priced identically
        s = self._meter_summary(valid, quality["interval_s"], tariff)
        meta = {"synthetic": False, "reading_kind": "imported", "imported": True}
        src = f"csv:{req.filename}"
        ms = [Measurement(run_id=run_id, metric=m, value=v, unit=METRIC_UNITS[m], source=src, metadata=dict(meta))
              for m, v in (("energy_kwh", s["energy_kwh"]), ("energy_cost", s["energy_cost"]), ("peak_power_kw", s["peak_power_kw"]), ("departure_charge_pct", req.departure_charge_pct))]  # fmt: skip
        ms += to_series(run_id, parsed, src)
        updated = await self.svc.ingest_measurements(exp_id, run_id, ms)
        return {"run": self._run_row(updated), "warnings": warnings, "summary": s}

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
        rec = narrative.recommendation(res, analysis, self._hh(res), alpha)
        return {
            "alpha": alpha, "summary": self._summary(res), "effects": fx, "recommendation": rec,
            "main_effects": [e.model_dump(mode="json") for e in analysis.main_effects if e.metric == "energy_cost"],
            "comparisons": [e.model_dump(mode="json") for e in analysis.effects if e.metric == "energy_cost"][:60],
            "descriptive": {k: v for k, v in analysis.descriptive.items() if k != "grid_power"}, "sample_sizes": analysis.sample_sizes, "warnings": analysis.warnings,
            "limitations": analysis.limitations, "assumptions": analysis.assumptions, "feasible_run_ids": analysis.feasible_run_ids, "pareto_run_ids": analysis.pareto_run_ids,
            "infeasible": [{"run_id": i["run_id"], "violations": i["violations"], "cost": i["objectives"].get("energy_cost")} for i in analysis.infeasible_runs],
            "best_run_id": analysis.best_run_id, "runs": {r["run_id"]: r for r in self.runs(exp_id)}, "excluded_runs": analysis.excluded_runs, "parameters": analysis.parameters,
        }  # fmt: skip

    async def optimise_suggest(self, exp_id: str, count: int, method: str) -> list[dict]:
        res = self._completed(exp_id)
        if len(res.runs) >= res.spec.run_budget:
            raise Conflict("run budget exhausted")
        recs = await asyncio.to_thread(self.svc.optimise, res.spec, res, count=count, method=method)
        if not recs:
            raise Conflict("the optimiser found no unseen feasible candidate")
        out = []
        for r in recs:
            d = r.model_dump(mode="json")
            d["schedule"] = narrative.schedule_text(r.configuration)
            out.append(d)
        return out

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

    async def report(self, exp_id: str) -> dict:
        res = self._completed(exp_id)
        analysis = await asyncio.to_thread(self.svc.analyse, res)
        report = await asyncio.to_thread(self.svc.report, res, analysis)
        rec = narrative.recommendation(res, analysis, self._hh(res))
        return {"report": report.model_dump(mode="json"), "recommendation": rec, "effects": rec["effects"], "simulated_notice": SIMULATED_NOTICE, "tariff": self._hh(res)["tariff"]}

    async def report_html(self, exp_id: str) -> str:
        data = await self.report(exp_id)
        res = self._result(exp_id)
        d = self.detail(exp_id)
        return report_html.render(res.spec.name, res.spec.hypothesis, data["tariff"], data["recommendation"], d["design"], d["runs"], data["effects"], data["report"])

    def csv_export(self, exp_id: str, kind: str) -> str:
        res = self._result(exp_id)
        if kind == "runs":
            return ReportEngine.to_csv(res)
        if kind == "measurements":
            return ReportEngine.measurements_csv(res)
        if kind == "effects":
            return ReportEngine.effects_csv(self.svc.analysis_engine.analyse(self._completed(exp_id)))
        raise NotFound(kind)

    @staticmethod
    def clock(h: float) -> str:
        return clock(h)
