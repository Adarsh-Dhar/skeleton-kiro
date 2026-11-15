import csv
import io
import json
from datetime import UTC, datetime

import pytest
from examples.third_domain_adapter.adapter import CoffeeRoastAdapter, roast_model
from examples.wattwise_meter_adapter.adapter import SyntheticWattwiseAdapter
from helpers import ScriptedAdapter, basic_spec

from trialforge import (
    Constraint,
    DerivedMetric,
    ExecutionError,
    ExperimentReport,
    ExperimentService,
    ExperimentSpec,
    Factor,
    Measurement,
    MeasurementDefinition,
    Objective,
    PluginError,
    PluginRegistry,
    ReportEngine,
    RunStatus,
    StoppingCriteria,
)


def coffee_spec(budget=30, **kw):
    return ExperimentSpec(
        name="roast",
        factors=[
            Factor(name="temperature", factor_type="continuous", lower=195, upper=215),
            Factor(name="duration", factor_type="continuous", lower=6, upper=14),
            Factor(name="airflow", factor_type="continuous", lower=0, upper=100),
        ],
        objectives=[Objective(metric="quality", direction="maximize")],
        measurement_definitions=[
            MeasurementDefinition(metric="quality", unit="score"),
            MeasurementDefinition(metric="moisture", unit="percent"),
            MeasurementDefinition(metric="colour", unit="index"),
        ],
        design_method=kw.pop("method", "ofat"),
        run_budget=budget,
        random_seed=21,
        **kw,
    )


# ---------------------------------------------------------------------- sequential optimisation
async def test_sequential_loop_extends_design_within_budget_and_improves():
    s = ExperimentService(CoffeeRoastAdapter())
    spec = coffee_spec(budget=24, method="random", design_options={"n_runs": 8})
    res = await s.run(spec)
    best0 = max(
        roast_model(r.configuration["temperature"], r.configuration["duration"], r.configuration["airflow"])["quality"]
        for r in res.runs
    )
    final, stop = await s.run_sequential(spec.experiment_id, iterations=4, batch_size=2, method="bayesian")
    assert len(final.runs) == 16 and stop.reason == "max_iterations" and final.status.value == "completed"
    best1 = max(
        roast_model(r.configuration["temperature"], r.configuration["duration"], r.configuration["airflow"])["quality"]
        for r in final.runs
    )
    assert best1 >= best0
    seq = [r for r in final.runs if r.metadata.get("sequential_iteration")]
    assert (
        len(seq) == 8
        and {r.metadata["sequential_iteration"] for r in seq} == {1, 2, 3, 4}
        and all(r.status == RunStatus.COMPLETED for r in seq)
    )
    recs = s.repository.list_recommendations(spec.experiment_id)
    assert len(recs) == 8 and all(r.prediction_kind in ("model_prediction", "search") for r in recs)
    assert s.repository.get_plan(spec.experiment_id).design_info["sequential_extensions"][0]["iteration"] == 1
    assert len(s.history(spec.experiment_id).recommendations()) == 8


async def test_sequential_loop_never_exceeds_budget_and_reports_stop_reason():
    s = ExperimentService(CoffeeRoastAdapter())
    spec = coffee_spec(budget=9, method="random", design_options={"n_runs": 6})
    await s.run(spec)
    final, stop = await s.run_sequential(spec.experiment_id, iterations=10, batch_size=2, method="random")
    assert len(final.runs) == 9 and stop.reason == "budget_exhausted"
    assert s.optimise(spec, final) == []


async def test_sequential_loop_stops_on_target_and_on_cancellation():
    s = ExperimentService(CoffeeRoastAdapter())
    spec = coffee_spec(budget=30, method="random", design_options={"n_runs": 6})
    await s.run(spec)
    _, stop = await s.run_sequential(spec.experiment_id, iterations=5, criteria=StoppingCriteria(target_value=0.0))  # already met
    assert stop.reason == "target_achieved"
    _, stop = await s.run_sequential(spec.experiment_id, iterations=5, criteria=StoppingCriteria(cancelled=lambda: True))
    assert stop.reason == "user_cancelled"
    assert len((await s.run_pending(spec.experiment_id)).runs) == 6


def test_add_runs_validates_configurations_and_budget():
    from trialforge import DesignError

    s = ExperimentService(ScriptedAdapter("a"))
    spec = basic_spec(3, run_budget=4)
    s.design(spec)
    with pytest.raises(DesignError, match="violates factor domain"):
        s.add_runs(spec.experiment_id, [{"x": 99}])
    s.add_runs(spec.experiment_id, [{"x": 1}])
    with pytest.raises(DesignError, match="remaining budget"):
        s.add_runs(spec.experiment_id, [{"x": 1}, {"x": 2}])


# ---------------------------------------------------------------------------- ingestion
async def test_externally_collected_measurements_are_ingested_and_runs_become_immutable():
    s = ExperimentService(ScriptedAdapter("a"))
    spec = basic_spec(2).model_copy(update={"measurement_definitions": [MeasurementDefinition(metric="y", unit="s")]})
    plan = s.design(spec)
    rid = plan.runs[0].run_id
    ms = [Measurement(run_id=rid, metric="y", value=2500.0, unit="ms", source="stopwatch-csv")]
    run = await s.ingest_measurements(spec.experiment_id, rid, ms)
    assert run.status == RunStatus.COMPLETED and run.processed_measurements[0].value == pytest.approx(2.5)
    assert run.measurements[0].raw_value == 2500.0 and run.measurements[0].source == "stopwatch-csv"
    with pytest.raises(ExecutionError, match="can no longer change"):
        await s.ingest_measurements(spec.experiment_id, rid, ms)
    other_spec = basic_spec(1, experiment_id="another-experiment")
    other_plan = s.design(other_spec)
    with pytest.raises(ExecutionError, match="does not belong"):  # a run id from a different experiment is refused
        await s.ingest_measurements(spec.experiment_id, other_plan.runs[0].run_id, ms)
    dup = await s.ingest_measurements(
        spec.experiment_id,
        plan.runs[1].run_id,
        [
            Measurement(run_id=plan.runs[1].run_id, metric="y", value=1.0, unit="s", timestamp=datetime(2026, 1, 1, tzinfo=UTC))
            for _ in range(2)
        ],
    )  # distinct measurements, same instant and value
    assert sum("duplicate" in m.quality_flags for m in dup.measurements) == 1 and dup.status == RunStatus.COMPLETED
    same = Measurement(run_id=plan.runs[1].run_id, metric="y", value=1.0, unit="s")
    s2 = ExperimentService(ScriptedAdapter("a"))
    p2 = s2.design(spec)
    r2 = await s2.ingest_measurements(
        spec.experiment_id, p2.runs[0].run_id, [same.model_copy(update={"run_id": p2.runs[0].run_id})] * 3
    )
    assert len(r2.measurements) == 1  # re-delivery of the same measurement id is idempotent


async def test_csv_import_end_to_end_for_wattwise_and_f1():
    from examples.f1_simulator_adapter.adapter import measurements_from_telemetry_csv
    from examples.wattwise_meter_adapter.adapter import Tariff, measurements_from_csv

    rows = ["timestamp,power"] + [
        f"2026-02-01T{h:02d}:00:00+00:00,{kw}" for h, kw in zip(range(0, 6), [1000, 1000, 3000, 3000, 500, 500])
    ]
    ms = measurements_from_csv(
        "RID", "\n".join(rows), tariff=Tariff(periods=((0, 24, 0.10),)), unit="W", departure_charge_pct=85.0
    )
    by = {m.metric: m for m in ms if m.metric != "grid_power"}
    assert (
        by["energy_kwh"].value == pytest.approx(9.0, rel=0)
        and by["peak_power_kw"].value == 3.0
        and by["energy_cost"].value == pytest.approx(0.9)
    )
    assert all(m.metadata["synthetic"] is False and m.metadata["reading_kind"] == "imported" for m in ms)
    spec = ExperimentSpec(
        name="imp",
        factors=[Factor(name="charge_start_hour", factor_type="discrete", values=[1])],
        objectives=[Objective(metric="energy_cost", direction="minimize")],
        constraints=[Constraint(metric="departure_charge_pct", minimum=80)],
        measurement_definitions=[
            MeasurementDefinition(metric="energy_cost", unit="currency"),
            MeasurementDefinition(metric="energy_kwh", unit="kWh"),
            MeasurementDefinition(metric="peak_power_kw", unit="kW"),
            MeasurementDefinition(metric="departure_charge_pct", unit="percent"),
            MeasurementDefinition(metric="grid_power", unit="kW", time_series=True, aggregation="max"),
        ],
        run_budget=1,
    )
    s = ExperimentService(SyntheticWattwiseAdapter())
    plan = s.design(spec)
    run = await s.ingest_measurements(
        spec.experiment_id, plan.runs[0].run_id, [m.model_copy(update={"run_id": plan.runs[0].run_id}) for m in ms]
    )
    assert run.status == RunStatus.COMPLETED and run.metadata["feasible"] is True
    assert {p.metric for p in run.processed_measurements} >= {"grid_power", "energy_cost"}
    with pytest.raises(Exception, match="row 2"):
        measurements_from_csv("r", "timestamp,power\nnot-a-time,3")
    tel = measurements_from_telemetry_csv("r", "sector_1,sector_2,sector_3,stability\n24.1,33.2,23.0,88\n24.0,33.1,23.1,")
    lap = [m.value for m in tel if m.metric == "lap_time"]
    assert lap == pytest.approx([80.3, 80.2]) and all(m.metadata["synthetic"] is False for m in tel)


# ------------------------------------------------------------------- extension points
async def test_custom_metric_objective_constraint_and_report_extension_without_core_changes():
    reg = PluginRegistry()
    reg.register("metric", "efficiency", lambda o: o["y"] / (1.0 + o["w"]))
    reg.register("objective", "neg_eff", lambda o: -o["y"] / (1.0 + o["w"]))
    reg.register("constraint", "w_positive", lambda cfg, out: out.get("w", 0) > 0)
    reg.register("report", "summary_line", lambda report, result: {"runs": len(result.runs), "name": report.name})
    spec = ExperimentSpec(
        name="plug",
        factors=[Factor(name="x", factor_type="discrete", values=[0, 1, 2])],
        objectives=[Objective(metric="custom", direction="minimize", function="neg_eff")],
        measurement_definitions=[MeasurementDefinition(metric="y", unit="u"), MeasurementDefinition(metric="w", unit="u")],
        derived_metrics=[DerivedMetric(name="eff", unit="u", function="efficiency")],
        constraints=[Constraint(kind="output", function="w_positive")],
        run_budget=10,
    )

    class TwoMetric(ScriptedAdapter):
        def _ms(self, run_id, cfg):
            return [
                Measurement(run_id=run_id, metric="y", value=10.0 + cfg["x"], unit="u"),
                Measurement(run_id=run_id, metric="w", value=1.0 + (3 if cfg["x"] == 2 else 0), unit="u"),
            ]

    s = ExperimentService(TwoMetric("a"), plugins=reg, report_engine=ReportEngine(plugins=reg, extensions=["summary_line"]))
    res = await s.run(spec)
    effs = {r.configuration["x"]: next(p.value for p in r.processed_measurements if p.metric == "eff") for r in res.runs}
    assert effs == {0: pytest.approx(5.0), 1: pytest.approx(5.5), 2: pytest.approx(12 / 5)}
    an = s.analyse(res)
    best = next(r for r in res.runs if r.run_id == an.best_run_id)
    assert best.configuration["x"] == 1 and all(r.metadata["feasible"] for r in res.runs)  # objective function drove the ranking
    rep = s.report(res, an)
    assert rep.extensions["summary_line"] == {"runs": 3, "name": "plug"}


def test_unregistered_plugin_references_rejected_at_validation():
    s = ExperimentService(ScriptedAdapter("a"), plugins=PluginRegistry())
    from trialforge import ValidationError

    spec = basic_spec(2).model_copy(update={"objectives": [Objective(metric="y", direction="minimize", function="ghost")]})
    with pytest.raises(ValidationError, match="objective function 'ghost' is not registered"):
        s.design(spec)


def test_plugin_registry_is_explicit_and_safe():
    reg = PluginRegistry()
    reg.register("metric", "m", lambda o: 1.0)
    with pytest.raises(PluginError, match="already registered"):
        reg.register("metric", "m", lambda o: 2.0)
    reg.register("metric", "m", lambda o: 2.0, replace=True)
    with pytest.raises(PluginError, match="unknown plugin kind"):
        reg.register("shell", "x", lambda: 0)
    with pytest.raises(PluginError, match="callable"):
        reg.register("metric", "n", "os.system('x')")
    with pytest.raises(PluginError, match="no metric plugin"):
        reg.get("metric", "absent")
    assert reg.names("metric") == ["m"] and reg.has("metric", "m") and not reg.has("objective", "m")

    @reg.transformation("double")
    def double(vals):
        return [2 * v for v in vals]

    assert reg.get("transformation", "double")([1]) == [2]
    reg.unregister("metric", "m")
    assert not reg.has("metric", "m")


# ------------------------------------------------------------------------------- reporting
async def test_report_contains_every_required_section_and_round_trips():
    s = ExperimentService(SyntheticWattwiseAdapter())
    spec = ExperimentSpec(
        name="wattwise",
        hypothesis="Charging after midnight lowers cost.",
        description="cost study",
        factors=[
            Factor(name="charge_start_hour", factor_type="discrete", values=[22, 0, 4, 6]),
            Factor(name="charge_power_kw", factor_type="continuous", lower=3, upper=11),
            Factor(name="cooling_setpoint", factor_type="discrete", values=[24]),
        ],
        objectives=[Objective(metric="energy_cost", direction="minimize")],
        constraints=[Constraint(metric="departure_charge_pct", minimum=80)],
        measurement_definitions=[
            MeasurementDefinition(metric="energy_cost", unit="currency"),
            MeasurementDefinition(metric="energy_kwh", unit="kWh"),
            MeasurementDefinition(metric="peak_power_kw", unit="kW"),
            MeasurementDefinition(metric="departure_charge_pct", unit="percent"),
        ],
        design_options={"replicates": 2},
        run_budget=40,
        random_seed=5,
    )
    res = await s.run(spec)
    s.optimise(spec, res, count=2, method="random")
    an = s.analyse(res)
    rep = s.report(res, an)
    assert (rep.hypothesis, rep.name) == ("Charging after midnight lowers cost.", "wattwise")  # 1 definition, 2 hypothesis
    assert [f.name for f in rep.variables] == ["charge_start_hour", "charge_power_kw", "cooling_setpoint"]  # 3 variables
    assert rep.objective[0].metric == "energy_cost" and rep.constraints[0].metric == "departure_charge_pct"  # 4, 5
    assert rep.design_method == "factorial" and rep.design_info["replicates"] == 2  # 6
    assert rep.planned_runs == 24 == len(rep.planned_run_details) and rep.completed_runs == 24 and rep.failed_runs == 0  # 7, 8, 9
    assert rep.invalid_measurements == [] and rep.measurement_summaries["energy_cost"]["count"] == 24  # 10, 11
    assert rep.treatment_effects and any(e.evidence == "sufficient" for e in rep.treatment_effects)  # 12, 13, 14
    assert rep.interactions and rep.anova and rep.regression and rep.model_diagnostics[0]["metric"] == "energy_cost"  # 15
    assert len(rep.optimisation_history) == 2 and rep.suggested_next_experiment == rep.optimisation_history[-1]  # 16, 18
    best = next(r for r in res.runs if r.run_id == rep.best_observed_run_id)
    assert rep.best_configuration == best.configuration and best.metadata["feasible"] is True  # 17
    assert (
        rep.constraint_violations and all(v["kind"] == "output" for v in rep.constraint_violations) and rep.infeasible_runs
    )  # 19
    assert rep.data_quality_issues is not None and rep.limitations  # 20, 21
    assert any("SYNTHETIC" in l for l in rep.limitations)
    rp = rep.reproducibility  # 22
    assert (
        len(rp["specification_hash"]) == 64
        and rp["random_seed"] == 5
        and rp["run_order"] == [r.run_id for r in sorted(res.runs, key=lambda r: r.order)]
    )
    assert rp["adapters"] == [{"name": "wattwise-meter", "version": "1.0", "synthetic": True}] and rep.report_version == "2.0"
    again = ReportEngine().to_json(rep)
    assert ExperimentReport.model_validate_json(again).model_dump() == rep.model_dump()
    assert json.loads(again)["report_version"] == "2.0"
    rep2 = s.report(res, an)
    assert rep2.reproducibility == rep.reproducibility
    assert len(s.repository.list_reports(spec.experiment_id)) == 2


async def test_csv_exports_label_failed_runs_and_quality():
    a = ScriptedAdapter("a", fail_first=1)
    s = ExperimentService(a)
    spec = basic_spec(2).model_copy(update={"run_budget": 10})
    bad = await s.run(spec)
    rows = list(csv.DictReader(io.StringIO(ReportEngine.to_csv(bad))))
    assert {r["status"] for r in rows} == {"failed"} and all("transient" in r["error"] and r["y"] == "" for r in rows)
    ok = await ExperimentService(ScriptedAdapter("a")).run(basic_spec(2, experiment_id="ok"))
    rows = list(csv.DictReader(io.StringIO(ReportEngine.to_csv(ok))))
    assert sorted(float(r["y"]) for r in rows) == [10.0, 11.0] and rows[0]["feasible"] in ("True", "")
    m = list(csv.DictReader(io.StringIO(ReportEngine.measurements_csv(ok))))
    assert m[0]["quality_status"] == "accepted" and m[0]["metric"] == "y"
    an = AnalysisEngineHelper.analyse(ok)
    e = list(csv.DictReader(io.StringIO(ReportEngine.effects_csv(an))))
    assert e and e[0]["metric"] == "y"


class AnalysisEngineHelper:
    @staticmethod
    def analyse(res):
        from trialforge import AnalysisEngine

        return AnalysisEngine().analyse(res)


def test_csv_formula_injection_is_neutralised():
    from trialforge.reporting.engine import _safe

    assert _safe("=cmd()") == "'=cmd()" and _safe("@x") == "'@x" and _safe(5) == 5 and _safe("ok") == "ok"


async def test_report_without_feasible_run_has_no_best_configuration():
    s = ExperimentService(ScriptedAdapter("a"))
    spec = basic_spec(2, constraints=[Constraint(metric="y", maximum=0.0)])
    res = await s.run(spec)
    rep = s.report(res)
    assert rep.best_configuration is None and any("No feasible observed run" in l for l in rep.limitations)
