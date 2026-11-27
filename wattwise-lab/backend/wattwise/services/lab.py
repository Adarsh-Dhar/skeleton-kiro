"""LabService: the WATTWISE workflow on top of the TRIALFORGE SDK.

The SDK does all experiment work; this class maps electricity concepts (tariffs, EV departure charge, appliance schedules, meter CSVs)
onto SDK objects.
"""

from __future__ import annotations

import asyncio
import math
import statistics
import uuid
from collections import defaultdict, deque
from datetime import UTC, date
from typing import Any
from zoneinfo import ZoneInfo

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
)

from db.config import resolve_db_url
from db.models.common import APP_WATTWISE
from db.scoped import ScopedExperimentRepository
from wattwise import SIMULATED_NOTICE
from wattwise.adapters import provenance as PV
from wattwise.adapters import tariff_model as TM
from wattwise.adapters.household_model import (
    METRIC_UNITS,
    STEP_H,
    STEPS,
    WINDOW_START,
    ChargerCapacityError,
    FactorConflict,  # noqa: F401  (re-exported: the API layer maps it to a validation error)
    MissingInput,
    appliance_key,
    baseline_setup,
    check_setup_keys,
    clock,
    factor_limits,
    household_catalog,
    simulate,
    with_baseline,
)
from wattwise.adapters.meter_adapter import MeterSimulatorAdapter
from wattwise.adapters.meter_csv import ParsedMeter, parse_meter_csv, to_series
from wattwise.adapters.meter_import import Reading, interpret
from wattwise.adapters.tariff_model import MissingStartDate  # noqa: F401  (re-exported: the API layer maps it to a validation error)
from wattwise.api.schemas import DayRequest, ExperimentRequest, MeterAttachRequest, MeterRequest
from wattwise.services import baseline, narrative, report_html
from wattwise.services.baseline import (  # noqa: F401  (re-exported: the API layer maps these to validation errors)
    BaselineOverlap,
    BaselineUnavailable,
    InvalidDayType,
    NoCompleteDay,
)
from wattwise.services.evidence import EvidenceStore
from wattwise.services.household import HouseholdStore
from wattwise.services.meter_library import MeterLibrary, options_from, usable_as_baseline
from wattwise.services.tariffs import TariffError, TariffStore  # noqa: F401  (TariffError re-exported)

ADAPTER_PREFIX = "wattwise-sim/"
GENERATORS = {3: ["C=AB"]}
NOTABLE = {"unit_converted": "unit converted", "statistical_outlier": "outlier", "sensor_warning": "sensor warning", "duplicate": "duplicate", "out_of_order": "out of order"}


class NotFound(KeyError):
    pass


class Conflict(RuntimeError):
    pass


EV_ONLY_FACTORS = ("charge_start", "charge_power_kw")


def measurement_definitions(has_ev: bool = True) -> list[MeasurementDefinition]:
    defs = [MeasurementDefinition(metric=m, unit=u, required=m in ("energy_cost", "energy_kwh", "peak_power_kw", "departure_charge_pct"))
            for m, u in METRIC_UNITS.items() if m != "grid_power" and (has_ev or m != "departure_charge_pct")]  # fmt: skip
    defs.append(MeasurementDefinition(metric="grid_power", unit="kW", time_series=True, sampling_interval_seconds=900, required=False, outlier_method="none"))
    return defs


class LabService:
    def __init__(self, db_path: str | None = None) -> None:
        """``db_path`` is for tests only; production uses the root database (``TRIALFORGE_DB`` / ``data/trialforge.db``)."""
        self.registry = AdapterRegistry()
        url = resolve_db_url(db_path)
        self.repo = ScopedExperimentRepository(APP_WATTWISE, url)
        self.engine = self.repo.db_engine
        self.svc = ExperimentService(registry=self.registry, repository=self.repo)
        self.runner = LocalBackgroundRunner(self.svc)
        self.tariffs = TariffStore(self.engine)
        self.households = HouseholdStore(self.engine)
        self.meters = MeterLibrary(self.engine)
        self.evidence = EvidenceStore(self.engine)

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
        """The tariff to price with: an inline tariff (from the bill) or one from the library. There is no default tariff."""
        if inline is not None:
            data = self.evidence.bind_all(inline.model_dump(mode="json", exclude_none=True))
            from wattwise.adapters.household_model import validate_tariff
            issues = validate_tariff(data)
            if issues:
                raise TariffError(issues)
            return {"id": "inline", **data, "source_status": PV.status(data.get("source"))}
        if not tariff_id:
            raise MissingInput("no tariff is configured: add the tariff from your electricity bill or your utility's tariff order")
        try:
            t = self.tariffs.get(tariff_id)
        except KeyError:
            raise NotFound(f"tariff {tariff_id}") from None
        PV.require_usable(t.get("source"), f"tariff '{t.get('name', tariff_id)}'")
        return t
    # ------------------------------------------------------------------ readiness and input sources
    def readiness(self, settings: dict[str, Any]) -> dict[str, Any]:
        """What the saved household still needs before projections can run. Nothing missing is ever filled in."""
        missing: list[dict[str, str]] = []
        warnings: list[str] = []
        tariff = None
        if not settings.get("tariff_id"):
            missing.append({"field": "tariff_id", "message": "add your tariff (from your bill or the utility's tariff order) and select it"})
        else:
            try:
                tariff = self.tariffs.get(settings["tariff_id"])
            except KeyError:
                missing.append({"field": "tariff_id", "message": f"the selected tariff '{settings['tariff_id']}' no longer exists"})
        if tariff is not None:
            st = tariff["source_status"]
            if not st["usable"]:
                missing.append({"field": "tariff_id", "message": f"tariff '{tariff['name']}' is a built-in example, not a real tariff: replace it"})
            warnings += [f"tariff '{tariff['name']}': {w}" for w in st["warnings"]]
        if settings.get("baseline_import_id"):
            try:
                meta = self.meters.get(settings["baseline_import_id"])
                usable_as_baseline(meta)
            except (NotFound, baseline.BaselineUnavailable) as exc:
                missing.append({"field": "baseline_import_id", "message": str(exc)})
        elif settings.get("base_load_kw") is None:
            missing.append({"field": "base_load_kw", "message": "import your meter readings (My usage) or enter your always-on load"})
        if settings.get("ev") is not None and settings.get("charger_power_kw") is None:
            missing.append({"field": "charger_power_kw", "message": "enter your charger's capacity from its nameplate"})
        if settings.get("ev") is None and not any(a.get("flexible") for a in settings.get("appliances") or []):
            missing.append({"field": "appliances", "message": "add at least one appliance you can move, or configure your EV"})
        return {"ready": not missing, "missing": missing, "warnings": warnings}
    @staticmethod
    def input_sources(hh: dict[str, Any]) -> list[dict[str, Any]]:
        """Snapshot of every input of a household with its source and status (stored with each experiment)."""
        out: list[dict[str, Any]] = []
        t = hh.get("tariff") or {}
        if t:
            out.append(PV.entry("tariff", t.get("source"), {"name": t.get("name"), "utility": t.get("utility"), "consumer_category": t.get("consumer_category"),
                                                            "currency": t.get("currency"), "periods": t.get("periods")}))
        ev = hh.get("ev")
        if ev is not None:
            out.append(PV.entry("ev", ev.get("source"), {k: ev.get(k) for k in ("battery_kwh", "efficiency", "initial_soc_pct", "desired_charge_pct",
                                                                               "departure_hour", "plug_in_hour")}))
        if hh.get("charger_capacity_kw") is not None:
            out.append(PV.entry("charger", hh.get("charger_source"), {"charger_power_kw": hh["charger_capacity_kw"]}))
        for a in hh.get("appliances") or []:
            out.append(PV.entry(f"appliances[{a.get('name')}]", a.get("source"), {k: a.get(k) for k in ("power_kw", "duration_h", "flexible", "fixed_start")}))
        bs = hh.get("baseline_source")
        if bs:
            out.append(PV.entry("meter_baseline", bs.get("source_record"), {"import_id": bs.get("import_id"), "file_name": bs.get("file_name"),
                                                                            "days_used": bs.get("days_used")}))
        elif hh.get("base_load_kw") is not None:
            out.append(PV.entry("base_load", hh.get("base_load_source"), {"base_load_kw": hh["base_load_kw"]}))
        return out

    def household(self, req: Any) -> dict[str, Any]:
        bind = self.evidence.bind_all  # evidence comes from the archive, never from the request
        hh = {"ev": bind(req.ev.model_dump(mode="json")) if req.ev is not None else None, "tariff": self.resolve_tariff(req.tariff_id, req.tariff),
              "base_load_kw": req.base_load_kw, "appliances": [bind(a.model_dump(mode="json")) for a in req.appliances]}  # fmt: skip
        if req.base_load_source is not None:
            hh["base_load_source"] = bind(req.base_load_source.model_dump(mode="json"))
        if getattr(req, "charger_capacity_kw", None) is not None:
            hh["charger_capacity_kw"] = float(req.charger_capacity_kw)
            if req.charger_source is not None:
                hh["charger_source"] = bind(req.charger_source.model_dump(mode="json"))
        start = getattr(req, "window_start_date", None)
        if start is not None:  # ISO string so the household stays JSON-serialisable; absent = derived reference date (old behaviour)
            hh["window_start_date"] = start.isoformat() if hasattr(start, "isoformat") else str(start)
        billing = getattr(req, "billing_cycle_consumption_kwh", None)
        if billing is not None:
            hh["billing_cycle_kwh"] = float(billing)
        if getattr(req, "baseline_import_id", None):
            cal = self.calibration(req.baseline_import_id, getattr(req, "day_type", None))
            hh["base_profile_kw"] = cal["base_profile_kw"]
            hh["baseline_source"] = {k: v for k, v in cal.items() if k != "base_profile_kw"}
            if billing is None and start is not None and hh["tariff"].get("tiers"):
                default = self.billing_cycle_default(req.baseline_import_id, date.fromisoformat(hh["window_start_date"]))
                hh["billing_cycle_kwh"] = default["kwh"]
                if default["notice"]:
                    hh["billing_cycle_notice"] = default["notice"]
        return hh

    def billing_cycle_default(self, import_id: str, day: date) -> dict[str, Any]:
        """Default billing-cycle consumption from a meter import; read errors give 0 kWh and a notice (Requirements 7.6, 7.7)."""
        meta = self.meters.get(import_id)  # unknown import: propagates as for the other meter endpoints
        try:
            readings = self.meters.readings(import_id)
        except Exception as exc:
            return {"kwh": 0.0, "notice": f"no default billing-cycle consumption could be derived: the file could not be read ({exc})"}
        return baseline.billing_cycle_default(readings, meta["timezone"], day)

    # ------------------------------------------------------------------ baseline overlap check (Requirement 10)
    @staticmethod
    def _modelled_loads(hh: dict[str, Any]) -> list[tuple[baseline.ModelledLoad, bool]]:
        """(load, declared_in_baseline) for every modelled appliance and the EV, at the baseline setup."""
        setup = with_baseline(hh, {})
        out: list[tuple[baseline.ModelledLoad, bool]] = []
        for a in hh["appliances"]:
            start = setup.get(appliance_key(a), a["fixed_start"]) if a["flexible"] else a["fixed_start"]
            steps = max(1, math.ceil(a["duration_h"] / STEP_H - 1e-9))
            out.append((baseline.ModelledLoad(a["name"], float(a["power_kw"]), steps, int(round((start - WINDOW_START) / STEP_H))),
                        bool(a.get("in_baseline"))))
        ev = hh.get("ev")
        if ev is not None:
            power = float(setup["charge_power_kw"])
            energy = ev["battery_kwh"] * (ev["desired_charge_pct"] - ev["initial_soc_pct"]) / 100
            if energy > 0 and power > 0:
                alone = {**hh, "appliances": [], "base_profile_kw": [0.0] * STEPS}  # EV load only
                ev_kw = tuple(float(s["ev"]) for s in simulate(setup, alone)["steps"])
                steps = max(1, math.ceil(energy / (power * ev["efficiency"]) / STEP_H - 1e-9))
                out.append((baseline.ModelledLoad("EV", power, steps, 0, ev_kw), bool(ev.get("in_baseline"))))
        return out

    def _baseline_check(self, req: Any, hh: dict[str, Any], readings: list | None, tz: str | None) -> dict[str, Any]:
        """Study flow of design section 11. Mutates ``hh["base_profile_kw"]`` when declared loads are subtracted."""
        loads = self._modelled_loads(hh)
        rec: dict[str, Any] = {"status": "not_applicable", "appliances_checked": [m.name for m, d in loads if not d], "appliances_subtracted": [],
                               "skipped_by_user_confirmation": False,
                               "thresholds": {"overlap_min_occurrences": baseline.OVERLAP_MIN_OCCURRENCES, "overlap_rise_fraction": baseline.OVERLAP_RISE_FRACTION},
                               "clipped": {"count": 0, "times": []}}  # fmt: skip
        if not getattr(req, "baseline_import_id", None):
            return rec
        series = baseline.quarter_hour_series(readings or [], tz or "UTC")
        if not series or all(v is None for _t, v in series):
            raise baseline.BaselineUnavailable("baseline is unavailable: the import has no quarter-hour data to check against the modelled appliances")
        if getattr(req, "baseline_excludes_modelled_appliances", False):
            return {**rec, "status": "skipped", "skipped_by_user_confirmation": True}
        declared = [m for m, d in loads if d]
        hits = baseline.check_overlap(series, [m for m, d in loads if not d])
        if hits:
            exc = baseline.BaselineOverlap(hits)
            exc.baseline_check = {**rec, "status": "blocked", "overlapping": hits}  # type: ignore[attr-defined]
            raise exc
        if declared:
            res = baseline.subtract_declared(hh["base_profile_kw"], declared)
            hh["base_profile_kw"] = res.profile_kw
            return {**rec, "status": "subtracted", "appliances_subtracted": res.subtracted, "clipped": res.clipped}
        return {**rec, "status": "passed"}

    # ------------------------------------------------------------------ historical meter library + baseline
    def meter_baseline(self, import_id: str, tariff_id: str | None = None) -> dict:
        meta = self.meters.get(import_id)
        tariff = self.resolve_tariff(tariff_id, None) if tariff_id else None
        out = baseline.analyse(self.meters.readings(import_id), meta["timezone"], tariff, source_label=meta.get("source_label"))
        out["import"] = {k: meta[k] for k in ("import_id", "file_name", "timezone", "reading_kind", "interval_minutes", "first_utc", "last_utc")}
        return out

    def calibration(self, import_id: str, day_type: str | None = None) -> dict:
        meta = self.meters.get(import_id)
        usable_as_baseline(meta)  # synthetic demo files stored by earlier versions are not a household's real load
        a = baseline.analyse(self.meters.readings(import_id), meta["timezone"], source_label=meta.get("source_label"))
        charger = self.households.get()["settings"].get("charger_power_kw")
        try:
            sel = baseline.select_profile(a, day_type)  # InvalidDayType / NoCompleteDay propagate (mapped to 422 by the API)
        except baseline.NoCompleteDay as exc:
            raise baseline.NoCompleteDay(f"meter import {import_id} cannot be used as a baseline: {exc}") from exc
        try:
            cal = baseline.calibration({**a, "profile_15min_kw": sel["profile_kw"]}, day_type=sel["day_type_used"], ev_charger_kw=charger,
                                       source_label=meta.get("source_label"))
        except ValueError as exc:
            raise ValueError(f"meter import {import_id} cannot be used as a baseline: {exc}") from exc
        if sel["fallback"]:
            cal["notes"] = [*cal["notes"], f"no complete {sel['requested']} day in this file: the all-days profile is used instead"]
        cal.update(days_used=sel["days_used"], day_type_used=sel["day_type_used"], requested_day_type=sel["requested"], fallback=sel["fallback"])
        return {**cal, "import_id": import_id, "file_name": meta["file_name"], "source_record": meta.get("source_record")}

    # ------------------------------------------------------------------ creation
    @staticmethod
    def _factor(name: str, low: float, high: float, meta: dict[str, Any], base: float) -> Factor:
        return Factor(name=name, factor_type="continuous", lower=low, upper=high, unit=meta["unit"],
                      default=base if low <= base <= high else round((low + high) / 2, 6))  # fmt: skip

    def build_spec(self, req: ExperimentRequest, exp_id: str, household: dict[str, Any], baseline_check: dict[str, Any] | None = None) -> ExperimentSpec:
        has_ev = household.get("ev") is not None
        catalog = household_catalog(household)
        base_setup = baseline_setup(household)
        per_appliance = req.appliance_start_mode == "per_appliance"
        notes: list[str] = []
        factors: list[Factor] = []
        for name, s in req.factors.items():
            if not has_ev and name in EV_ONLY_FACTORS:
                continue  # no EV: nothing to schedule (the default factor dict still lists these)
            if per_appliance and name == "appliance_start":
                # the shared entry is a range template: one factor per flexible appliance, unless an explicit per-appliance entry overrides it
                for app in household["appliances"]:
                    key = appliance_key(app) if app.get("flexible") else None
                    if key is None or key in req.factors:
                        continue
                    if s.enabled:
                        factors.append(self._factor(key, s.low, s.high, catalog[key], base_setup.get(key, (s.low + s.high) / 2)))
                continue
            if not s.enabled:
                continue
            meta = catalog[name]
            if meta["low"] >= meta["high"]:  # zero-width range (charger capacity at the 1.4 kW minimum): fixed, not designed
                notes.append(f"{name} fixed at {meta['low']:g} {meta['unit']}: the allowed range has zero width")
                continue
            factors.append(self._factor(name, s.low, s.high, meta, base_setup.get(name, (s.low + s.high) / 2)))
        check_setup_keys({f.name: 0.0 for f in factors})  # FactorConflict before anything is stored
        k, d = len(factors), req.design
        method = {"full_factorial": "factorial"}.get(d.method, d.method)
        if k < 2 and method != "ofat":  # factorial-type designs need two factors; the SDK's one-factor design is ofat
            method = "ofat"
            notes.append("design method changed to ofat: only one factor")
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
            experiment_id=exp_id, name=req.name, hypothesis=req.hypothesis, description="WATTWISE LAB experiment: projected outcomes computed from the household's configured inputs (not measurements).",
            factors=factors, objectives=objectives,
            constraints=[Constraint(metric="departure_charge_pct", kind="output", minimum=req.ev.desired_charge_pct)] if has_ev else [],
            measurement_definitions=measurement_definitions(has_ev), design_method=method, design_options=opts,
            adapter_name=ADAPTER_PREFIX + exp_id, run_budget=d.run_budget, random_seed=d.seed,
            execution=ExecutionPolicy(max_retries=req.simulator.max_retries, max_concurrency=4, run_timeout_seconds=120),
            metadata={"app": "wattwise-lab", "simulated": True, "desired_charge_pct": req.ev.desired_charge_pct if has_ev else None,
                      "baseline": "calibrated" if household.get("base_profile_kw") else "owner_entered_constant",
                      "lab": {"household": household, "input_sources": self.input_sources(household), **req.simulator.model_dump(), **({"notes": notes} if notes else {}),
                                **({"baseline_check": baseline_check} if baseline_check is not None else {})}},
        )  # fmt: skip

    def create(self, req: ExperimentRequest) -> dict:
        exp_id = req.experiment_id or "ww-" + uuid.uuid4().hex[:8]
        if self.repo.has_experiment(exp_id) or self.repo.exists_anywhere(exp_id):
            raise Conflict(f"an experiment with id '{exp_id}' already exists")
        readings = tz = None
        if req.baseline_import_id:
            tz = self.meters.get(req.baseline_import_id)["timezone"]
            readings = self.meters.readings(req.baseline_import_id)
            if not readings:
                raise baseline.BaselineUnavailable("baseline is unavailable: the meter import has no readings")
        household = self.household(req)
        check = self._baseline_check(req, household, readings, tz)  # BaselineOverlap / BaselineUnavailable before anything is stored
        spec = self.build_spec(req, exp_id, household, check)
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

    LEGACY_NOTICE = ("created before the zero-fabricated-data policy: it was built on built-in example inputs (example household, "
                     "synthetic load profile, simulated noise), so its results are not about your household and no saving is computed. "
                     "Create a new experiment from your configured inputs.")
    @staticmethod
    def _hh(res: ExperimentResult) -> dict[str, Any]:
        lab = res.spec.metadata["lab"]
        if "input_sources" not in lab:  # legacy experiment: never treated as real-input results
            return {**lab["household"], "legacy_inputs": True}
        return lab["household"]

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
        base = None if hh.get("legacy_inputs") else simulate(baseline_setup(hh), hh)["metrics"]
        catalog = household_catalog(hh)
        kwh = sum(r["outcomes"].get("energy_kwh", 0.0) for r in rows if r["status"] == "completed")
        return {
            "id": spec.experiment_id, "name": spec.name, "hypothesis": spec.hypothesis, "status": res.status.value, "created_at": spec.created_at.isoformat(),
            "design_method": res.plan.method, "run_budget": spec.run_budget, "runs_total": len(res.runs), "counts": counts,
            "desired_charge_pct": spec.metadata.get("desired_charge_pct"), "tariff": hh["tariff"]["name"], "simulated_kwh": round(kwh, 1),
            "factors": [{"name": f.name, "low": f.lower, "high": f.upper, "unit": f.unit, "label": catalog[f.name]["label"], "clock": catalog[f.name]["clock"]}
                        for f in spec.factors],
            "baseline_cost": round(base["energy_cost"], 3) if base else None,
            "legacy_inputs": bool(hh.get("legacy_inputs")), **({"legacy_notice": self.LEGACY_NOTICE} if hh.get("legacy_inputs") else {}),
            "best": {"run_id": best["run_id"], "cost": best["outcomes"]["energy_cost"], "kwh": best["outcomes"].get("energy_kwh"), "peak": best["outcomes"].get("peak_power_kw"),
                     "charge": best["outcomes"].get("departure_charge_pct"), "schedule": best["schedule"], "configuration": best["configuration"],
                     "saving_pct": round(100 * (base["energy_cost"] - best["outcomes"]["energy_cost"]) / base["energy_cost"], 1) if base else None} if best else None,
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
        d["simulator"] = {k: v for k, v in res.spec.metadata["lab"].items() if k not in ("household", "notes", "baseline_check", "input_sources")}
        if res.spec.metadata["lab"].get("notes"):
            d["notes"] = list(res.spec.metadata["lab"]["notes"])
        d["baseline_check"] = res.spec.metadata["lab"].get("baseline_check")  # None for studies created before the overlap check existed
        d["input_sources"] = res.spec.metadata["lab"].get("input_sources")  # snapshot taken when the experiment was created
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
        hh = self.household(req)
        check_setup_keys(req.setup)  # FactorConflict: shared and per-appliance start times in one setup
        # EV-only keys stay valid for a no-EV household (the model ignores them), so a client can send a full setup
        catalog = factor_limits(hh.get("charger_capacity_kw"), hh["appliances"], True)
        cap = hh.get("charger_capacity_kw")
        for k, v in req.setup.items():
            if k not in catalog or not (catalog[k]["low"] <= v <= catalog[k]["high"]):
                if k == "charge_power_kw" and cap is not None and v > cap and hh["ev"] is not None and v >= catalog[k]["low"]:
                    raise ChargerCapacityError(f"charging power {v:g} kW exceeds the charger capacity {cap:g} kW")
                raise ValueError(f"setup.{k} is not a valid schedule parameter value")
        return {**self._profile(simulate(req.setup, hh)), "tariff": hh["tariff"], "setup": self._setup_view(with_baseline(hh, req.setup), hh),
                "simulated": True, "result_kind": "projected"}

    @staticmethod
    def _setup_view(setup: dict[str, float], hh: dict[str, Any]) -> dict[str, float]:
        """The setup as shown to the user: without EV-only values when the household has no EV."""
        return setup if hh.get("ev") is not None else {k: v for k, v in setup.items() if k not in EV_ONLY_FACTORS}

    def run_profile(self, exp_id: str, run_id: str) -> dict:
        res = self._result(exp_id)
        run = next((r for r in res.runs if r.run_id == run_id), None)
        if run is None:
            raise NotFound(run_id)
        hh = self._hh(res)
        return {**self._profile(simulate(run.configuration, hh)), "tariff": hh["tariff"], "setup": self._setup_view(with_baseline(hh, run.configuration), hh),
                "run_id": run_id, "model_profile": True}

    # ------------------------------------------------------------------ retiring experiments (nothing is deleted)
    RETIRED_APP = "wattwise-lab:retired"
    def retire(self, exp_id: str) -> dict:
        """Take an experiment out of WATTWISE's lists without deleting it: its owner row moves to ``wattwise-lab:retired``."""
        res = self._result(exp_id)
        if any(r.status == RunStatus.RUNNING for r in res.runs):
            raise Conflict("cancel the running schedules before retiring this experiment")
        summary = self._summary(res)
        from sqlalchemy import text
        with self.engine.begin() as c:
            c.execute(text("UPDATE app_experiment_owner SET app = :r WHERE experiment_id = :i AND app = :a"),
                      {"r": self.RETIRED_APP, "i": exp_id, "a": "wattwise-lab"})
        self.repo._owned.discard(exp_id)
        if res.spec.adapter_name in self.registry:
            self.registry.unregister(res.spec.adapter_name)
        return {"retired": exp_id, "name": summary["name"], "legacy_inputs": summary["legacy_inputs"]}
    def retired(self) -> list[dict]:
        from sqlalchemy import text
        with self.engine.connect() as c:
            ids = [r[0] for r in c.execute(text("SELECT experiment_id FROM app_experiment_owner WHERE app = :r"), {"r": self.RETIRED_APP})]
        return [{"id": i} for i in ids]
    def unretire(self, exp_id: str) -> dict:
        from sqlalchemy import text
        with self.engine.begin() as c:
            n = c.execute(text("UPDATE app_experiment_owner SET app = 'wattwise-lab' WHERE experiment_id = :i AND app = :r"),
                          {"i": exp_id, "r": self.RETIRED_APP}).rowcount
        if not n:
            raise NotFound(f"retired experiment {exp_id}")
        return {"restored": exp_id}
    def baseline_profile(self, exp_id: str) -> dict:
        """Today's schedule projected on the experiment's own stored inputs (including a measured baseline profile)."""
        res = self._result(exp_id)
        hh = self._hh(res)
        if hh.get("legacy_inputs"):
            raise Conflict(f"experiment {exp_id} was {self.LEGACY_NOTICE}")
        return {**self._profile(simulate({}, hh)), "tariff": hh["tariff"], "setup": self._setup_view(baseline_setup(hh), hh), "result_kind": "projected"}
    def overview(self) -> dict:
        exps = self.list()
        best = [e["best"]["saving_pct"] for e in exps if e["best"] and e["best"]["saving_pct"] is not None]
        settings = self.households.get()["settings"]
        ready = self.readiness(settings)
        day = None
        if ready["ready"]:  # the household's own day; there is no example household to fall back to
            from wattwise.api.schemas import DayRequest
            s = settings
            req = DayRequest(tariff_id=s["tariff_id"], ev=s["ev"], appliances=s["appliances"], base_load_kw=s["base_load_kw"], base_load_source=s["base_load_source"],
                             charger_capacity_kw=s["charger_power_kw"], charger_source=s["charger_source"], baseline_import_id=s["baseline_import_id"])
            hh = self.household(req)
            day = {**self._profile(simulate({}, hh)), "tariff": hh["tariff"], "setup": baseline_setup(hh), "result_kind": "projected"}
        return {"experiments": exps, "kpis": {"experiments": len(exps), "runs": sum(e["counts"]["completed"] for e in exps), "best_saving_pct": max(best) if best else None,
                                              "simulated_kwh": round(sum(e["simulated_kwh"] for e in exps), 1)},
                "baseline_day": day, "readiness": ready, "notice": SIMULATED_NOTICE}  # fmt: skip

    # ------------------------------------------------------------------ execution
    def _refuse_legacy(self, res: ExperimentResult) -> None:
        if self._hh(res).get("legacy_inputs"):
            raise Conflict(f"experiment {res.spec.experiment_id} was {self.LEGACY_NOTICE}")
    def execute(self, exp_id: str) -> dict:
        res = self._result(exp_id)
        self._refuse_legacy(res)
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
        self._refuse_legacy(res)
        failed = [r for r in res.runs if r.status == RunStatus.FAILED]
        if not failed:
            raise Conflict("no failed runs to retry")
        for r in failed:
            self.svc.retry_run(exp_id, r.run_id)
        self._register_adapter(res.spec)
        self.runner.submit(exp_id)
        return {"retried": len(failed)}

    # ------------------------------------------------------------------ smart-meter CSV
    def _meter_read(self, csv_text: str, unit: str, options: dict | None = None) -> tuple[Any, list[Measurement], list[str], dict[str, Any]]:
        """Simple ``timestamp,power_kw|power_w`` parsing, or (when ``options`` is given) the configurable interpreter used by
        the historical meter library: column mapping, time zones/DST, interval energy, cumulative registers. With options,
        timestamps are converted to the chosen local time zone so readings are priced in the right tariff period."""
        interpretation: dict[str, Any] = {}
        own_minutes: dict[Any, deque[float]] = defaultdict(deque)  # start (UTC) -> the reading's own interval, in file order
        if options is not None:
            opt = options_from(options)
            res = interpret(csv_text, opt)
            tz = ZoneInfo(opt.timezone)
            for r in res.readings:
                own_minutes[r.start_utc.astimezone(UTC)].append(r.interval_minutes)
            parsed = ParsedMeter(readings=[(r.start_utc.astimezone(tz), r.kw) for r in res.readings], unit="kW")
            uneven = sum(1 for r in res.readings if abs(r.interval_minutes - (res.interval_minutes or r.interval_minutes)) > 1e-6)
            parsed.errors = [*res.warnings] + ([f"{uneven} reading(s) span an irregular interval (e.g. a gap in a cumulative register); "
                                                "each is priced for its own interval"] if uneven else [])
            interpretation = {"interpretation": res.options["resolved"], "summary": res.summary(), "row_log": res.row_log, "gaps": res.gaps,
                              "timezone": opt.timezone}
        else:
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
        valid = sorted(valid, key=lambda m: m.timestamp)
        # One Reading per valid measurement, each with its own interval (the file's typical interval for simple CSVs).
        priced: list[Reading] = []
        for i, m in enumerate(valid):
            q = own_minutes.get(m.timestamp.astimezone(UTC))
            minutes = q.popleft() if q else interval / 60
            kw = float(m.value)  # kW (SDK converted W)
            priced.append(Reading(start_utc=m.timestamp.astimezone(UTC), interval_minutes=minutes, kwh=kw * minutes / 60, kw=kw, original_ts=m.timestamp.isoformat(), source_row=i))
        return parsed, valid, warnings + parsed.errors, quality | {"interval_s": interval, "_readings": priced, **interpretation}

    def _meter_summary(self, valid: list[Measurement], interval_s: float, tariff: dict[str, Any], readings: list[Reading] | None = None) -> dict[str, Any]:
        """Price each reading for ``[start, start + its own interval)`` through ``price_reading`` (gaps cost nothing). ``interval_s`` (the
        typical interval) is reported only; it prices nothing."""
        if readings is None:  # fixed-interval fallback for callers without Reading objects
            readings = [Reading(m.timestamp, interval_s / 60, float(m.value) * interval_s / 3600, float(m.value), "", i) for i, m in enumerate(valid)]
        starts = [m.timestamp for m in valid]
        charge = credit = energy = peak = 0.0
        cum = 0.0  # import carried across readings, as the previous summarise() loop did
        by_period: dict[str, dict[str, float]] = {}
        hourly: dict[int, list[float]] = {}
        for start, r in zip(starts, readings, strict=True):
            energy += r.kwh
            peak = max(peak, r.kw)
            hourly.setdefault(start.hour, []).append(r.kw)
            for sl in TM.price_reading(tariff, start, r.interval_minutes * 60, r.kwh, cum):
                cum += max(sl.kwh, 0.0)
                charge += sl.charge
                credit += sl.credit
                d = by_period.setdefault(sl.label, {"kwh": 0.0, "cost": 0.0})
                d["kwh"] += sl.kwh
                d["cost"] += sl.charge - sl.credit
        parts = TM.finish(tariff, charge, credit, peak, 1.0, single_day=True)
        hours = sum(r.interval_minutes for r in readings) / 60
        return {"energy_kwh": energy, "energy_cost": parts["total"], "energy_charge": charge, "demand_charge": parts["demand_charge"],
                "peak_power_kw": peak, "average_kw": sum(r.kw for r in readings) / len(readings), "readings": len(readings),
                "hours_covered": hours, "interval_minutes": interval_s / 60,
                "cost_by_period": {k: {a: round(b, 4) for a, b in v.items()} for k, v in by_period.items()},
                "hourly_profile": [{"hour": h, "kw": round(sum(v) / len(v), 3)} for h, v in sorted(hourly.items())]}  # fmt: skip

    def meter_preview(self, req: MeterRequest) -> dict:
        parsed, valid, warnings, quality = self._meter_read(req.csv, req.unit, req.options)
        tariff = self.resolve_tariff(req.tariff_id, req.tariff)
        step = max(1, len(valid) // 480)
        return {"summary": self._meter_summary(valid, quality["interval_s"], tariff, quality.pop("_readings")), "quality": quality, "warnings": warnings, "errors": parsed.errors, "tariff": tariff,
                "unit": parsed.unit, "series": [{"t": m.timestamp.isoformat(), "kw": round(float(m.value), 4)} for m in valid[::step]], "reading_kind": "imported",
                "provenance": "historical_measured", "source_label": "unknown"}  # fmt: skip  (ad-hoc CSV: no library record, so unlabelled)

    async def attach_meter(self, exp_id: str, run_id: str, req: MeterAttachRequest) -> dict:
        res = self._result(exp_id)
        if not any(r.run_id == run_id for r in res.runs):
            raise NotFound(run_id)
        parsed, valid, warnings, quality = self._meter_read(req.csv, req.unit, req.options)
        tariff = self._hh(res)["tariff"]  # the experiment's own tariff, so imported and simulated runs are priced identically
        s = self._meter_summary(valid, quality["interval_s"], tariff, quality.pop("_readings"))
        meta = {"synthetic": False, "reading_kind": "imported", "imported": True}
        src = f"csv:{req.filename}"
        ms = [Measurement(run_id=run_id, metric=m, value=v, unit=METRIC_UNITS[m], source=src, metadata=dict(meta))
              for m, v in (("energy_kwh", s["energy_kwh"]), ("energy_cost", s["energy_cost"]), ("peak_power_kw", s["peak_power_kw"]), ("departure_charge_pct", req.departure_charge_pct))]  # fmt: skip
        ms += to_series(run_id, parsed, src)
        updated = await self.svc.ingest_measurements(exp_id, run_id, ms)
        return {"run": self._run_row(updated), "warnings": warnings, "summary": s, "provenance": "historical_measured", "source_label": "unknown"}

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
        self._refuse_legacy(res)
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
        return {"report": report.model_dump(mode="json"), "recommendation": rec, "effects": rec["effects"], "simulated_notice": SIMULATED_NOTICE,
                "tariff": self._hh(res)["tariff"], "input_sources": res.spec.metadata["lab"].get("input_sources"), "result_kind": "projected"}

    async def report_html(self, exp_id: str) -> str:
        data = await self.report(exp_id)
        res = self._result(exp_id)
        d = self.detail(exp_id)
        return report_html.render(res.spec.name, res.spec.hypothesis, data["tariff"], data["recommendation"], d["design"], d["runs"], data["effects"], data["report"],
                                  data["input_sources"])

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
