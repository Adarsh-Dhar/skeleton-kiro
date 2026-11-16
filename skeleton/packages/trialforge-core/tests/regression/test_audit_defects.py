"""One test per defect identified in the 0.1.0 audit. Each fails on the 0.1.0 behaviour."""

import pytest
from conftest import build_result, simple_spec
from helpers import ScriptedAdapter, basic_spec

from trialforge import (
    AdapterRegistry,
    AnalysisEngine,
    Constraint,
    ExperimentService,
    Factor,
    MeasurementDefinition,
    Optimizer,
)
from trialforge.measurements.units import convert


def test_defect_single_adapter_service_now_selects_from_registry():
    reg = AdapterRegistry()
    reg.register(ScriptedAdapter("one"))
    reg.register(ScriptedAdapter("two"))
    s = ExperimentService(registry=reg)
    assert s.registry.select(basic_spec(adapter_name="two"))[0] == "two"


def test_defect_specs_cache_removed_and_spec_comes_from_repository():
    s = ExperimentService(ScriptedAdapter("a"))
    assert not hasattr(s, "_specs")
    spec = basic_spec(2)
    plan = s.design(spec)
    s._spec_overrides.clear()
    assert s.get_spec(spec.experiment_id).experiment_id == spec.experiment_id and plan.runs


async def test_defect_completed_runs_are_persisted_automatically_without_explicit_save():
    s = ExperimentService(ScriptedAdapter("a"))
    spec = basic_spec(2)
    await s.run(spec)
    stored = s.repository.list_runs(spec.experiment_id)
    assert len(stored) == 2 and all(r.status.value == "completed" and r.measurements and r.processed_measurements for r in stored)
    assert s.repository.get_status(spec.experiment_id).value == "completed"


def test_defect_treatments_were_pooled_into_one_group():
    s = simple_spec([Factor(name="a", factor_type="discrete", values=[0, 1, 2])])
    rows = [({"a": 0}, 10.0), ({"a": 0}, 10.2), ({"a": 1}, 15.0), ({"a": 1}, 15.2), ({"a": 2}, 5.0), ({"a": 2}, 5.2)]
    effects = AnalysisEngine().analyse(build_result(s, rows, control={"a": 0})).effects
    assert sorted(round(e.mean_difference, 3) for e in effects) == [-5.0, 5.0]  # 0.1.0 reported a single pooled value of 0.0


def test_defect_confidence_interval_used_normal_approximation():
    s = simple_spec([Factor(name="a", factor_type="discrete", values=[0, 1])])
    rows = [({"a": 0}, v) for v in (1.0, 2.0, 3.0)] + [({"a": 1}, v) for v in (4.0, 5.0, 6.0)]
    e = AnalysisEngine().analyse(build_result(s, rows, control={"a": 0})).effects[0]
    lo, hi = e.confidence_interval_95
    assert (hi - lo) / 2 == pytest.approx(2.7764 * e.standard_error, rel=1e-3)  # t(0.975, df=4), not 1.96
    assert (hi - lo) / 2 > 1.96 * e.standard_error * 1.3


def test_defect_interactions_and_per_factor_effects_were_missing():
    s = simple_spec(
        [Factor(name="a", factor_type="discrete", values=[0, 1]), Factor(name="b", factor_type="discrete", values=[0, 1])]
    )
    rows = [({"a": a, "b": b}, 2.0 * a * b + d) for a in (0, 1) for b in (0, 1) for d in (-0.1, 0.1)]
    r = AnalysisEngine().analyse(build_result(s, rows, control={"a": 0, "b": 0}))
    assert r.interactions[0].estimate == pytest.approx(2.0) and {e.factor for e in r.main_effects} == {"a", "b"}


def test_defect_output_constraints_ignored_when_choosing_best_configuration():
    s = simple_spec(
        [Factor(name="a", factor_type="discrete", values=[0, 1, 2])],
        measurement_definitions=[MeasurementDefinition(metric="y", unit="u"), MeasurementDefinition(metric="g", unit="u")],
        constraints=[Constraint(metric="g", minimum=1)],
    )
    rows = [({"a": 0}, {"y": 5.0, "g": 2.0}), ({"a": 1}, {"y": 1.0, "g": 0.0}), ({"a": 2}, {"y": 3.0, "g": 2.0})]
    res = AnalysisEngine().analyse(build_result(s, rows, control={"a": 0}))
    assert res.best_run_id == "r2"  # r1 is lower y but violates g >= 1


def test_defect_inverse_distance_heuristic_was_presented_as_expected_improvement():
    s = simple_spec([Factor(name="x", factor_type="continuous", lower=0, upper=1)])
    rows = [({"x": 0.1 * i}, float(i % 3)) for i in range(8)]
    res = build_result(s, rows, control={"x": 0.0}, method="random")
    h = Optimizer().suggest(s, res, method="heuristic")[0]
    assert h.expected_improvement is None and h.predicted_value is None and h.prediction_kind == "heuristic_score"
    b = Optimizer().suggest(s, res, method="bayesian")[0]
    assert b.prediction_kind == "model_prediction" and b.expected_improvement is not None


def test_defect_unit_conversion_was_missing():
    assert convert(1500, "ms", "s") == 1.5 and convert(2, "kW", "W") == 2000 and convert(1, "kWh", "Wh") == 1000


def test_defect_example_modules_and_documented_paths_exist():
    import importlib

    for mod in (
        "examples.f1_simulator_adapter.adapter",
        "examples.wattwise_meter_adapter.adapter",
        "examples.third_domain_adapter.adapter",
    ):
        importlib.import_module(mod)


def test_defect_found_by_application_integration_failed_design_must_not_leave_an_orphan_experiment():
    """0.2.0 saved the spec before designing, so a rejected design (e.g. budget too small) left a plan-less experiment behind."""
    from trialforge import DesignError, ExperimentSpec

    s = ExperimentService(ScriptedAdapter("a"))
    spec = basic_spec(5, run_budget=2)
    with pytest.raises(DesignError, match="exceeds budget"):
        s.design(spec)
    assert s.repository.list_ids() == [] and not s.repository.has_experiment(spec.experiment_id)
    ok = basic_spec(2, run_budget=5).model_copy(update={"experiment_id": spec.experiment_id})
    assert isinstance(ok, ExperimentSpec) and len(s.design(ok).runs) == 2        # the same id can be used once the spec is valid
