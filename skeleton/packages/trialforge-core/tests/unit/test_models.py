import pytest
from pydantic import ValidationError as PydanticValidationError

from trialforge import (
    Constraint,
    DerivedMetric,
    ExecutionPolicy,
    ExperimentSpec,
    Factor,
    InvalidTransitionError,
    MeasurementDefinition,
    Objective,
    Run,
    RunStatus,
)
from trialforge.models.core import EXPERIMENT_TRANSITIONS, RUN_TRANSITIONS, ExperimentStatus


def spec(**kw):
    base = dict(
        name="s",
        factors=[
            Factor(name="x", factor_type="continuous", lower=0, upper=10, unit="mm"),
            Factor(name="mode", factor_type="categorical", values=["a", "b"]),
        ],
        objectives=[Objective(metric="y", direction="minimize")],
        measurement_definitions=[MeasurementDefinition(metric="y", unit="u"), MeasurementDefinition(metric="z", unit="u")],
        run_budget=10,
    )
    base.update(kw)
    return ExperimentSpec(**base)


def test_factor_types_ranges_units_defaults_and_candidates():
    f = Factor(name="x", factor_type="continuous", lower=0, upper=10, unit="mm", default=5)
    assert f.candidates(3) == [0, 5.0, 10.0] and f.unit == "mm" and f.default == 5
    assert f.accepts(10) and not f.accepts(10.1) and not f.accepts(True)
    d = Factor(name="d", factor_type="discrete", values=[1, 2, 3])
    assert d.candidates() == [1, 2, 3] and d.accepts(2) and not d.accepts(2.5)
    c = Factor(name="c", factor_type="categorical", values=["red", "blue"])
    assert c.accepts("red") and not c.accepts("green")
    assert Factor(name="k", factor_type="continuous", lower=0, upper=1, validation_rule=lambda v: v < 0.5).accepts(0.4)
    assert not Factor(name="k", factor_type="continuous", lower=0, upper=1, validation_rule=lambda v: v < 0.5).accepts(0.6)


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(factor_type="continuous", lower=5, upper=1),
        dict(factor_type="continuous", lower=0, upper=float("inf")),
        dict(factor_type="continuous", values=[1, 2]),
        dict(factor_type="discrete", values=[]),
        dict(factor_type="discrete", values=[1, 1]),
        dict(factor_type="discrete", values=["a"]),
        dict(factor_type="categorical", values=["a", 1]),
        dict(factor_type="discrete", values=[1, 2], lower=0),
        dict(factor_type="discrete", values=[1, 2], default=3),
        dict(factor_type="complex", values=[1]),
    ],
)
def test_invalid_factor_definitions_rejected(kwargs):
    with pytest.raises((ValueError, PydanticValidationError)):
        Factor(name="x", **kwargs)


def test_factor_name_pattern_and_coding():
    with pytest.raises(ValueError):
        Factor(name="1bad", factor_type="discrete", values=[1])
    f = Factor(name="x", factor_type="continuous", lower=10, upper=30)
    assert f.decode(-1) == 10 and f.decode(1) == 30 and f.decode(0) == 20
    assert f.encode(20) == 0 and f.encode(30) == 1


def test_objective_directions_weights_and_targets():
    assert Objective(metric="y", direction="target", target=3).target == 3
    with pytest.raises(ValueError):
        Objective(metric="y", direction="target")
    with pytest.raises(ValueError):
        Objective(metric="y", direction="minimize", target=1)
    with pytest.raises(ValueError):
        Objective(metric="y", direction="minimize", weight=0)
    s = spec(
        objectives=[
            Objective(metric="y", direction="minimize", weight=2, priority=1),
            Objective(metric="z", direction="maximize", priority=2),
        ]
    )
    assert len(s.objectives) == 2


def test_input_and_output_constraints_are_distinguished():
    s = spec(
        constraints=[
            Constraint(metric="x", maximum=8),
            Constraint(metric="z", minimum=1),
            Constraint(metric="y", kind="output", maximum=5),
        ]
    )
    assert [c.metric for c in s.input_constraints()] == ["x"]
    assert sorted(c.metric for c in s.output_constraints()) == ["y", "z"]


def test_constraint_validation_errors():
    for kw in (
        dict(metric="x"),
        dict(metric="x", minimum=5, maximum=1),
        dict(metric="x", equals=1, minimum=0),
        dict(metric="x", allowed_values=[]),
        dict(metric="x", minimum=float("nan")),
    ):
        with pytest.raises(ValueError):
            Constraint(**kw)
    with pytest.raises(ValueError):
        Constraint(metric="x", function="f")  # function constraints must declare kind


@pytest.mark.parametrize(
    "kwargs,match",
    [
        (dict(objectives=[Objective(metric="nope", direction="minimize")]), "not defined"),
        (dict(constraints=[Constraint(metric="ghost", minimum=1)]), "not defined"),
        (dict(constraints=[Constraint(metric="x", minimum=9), Constraint(metric="x", maximum=2)]), "conflicting"),
        (dict(constraints=[Constraint(metric="x", minimum=11)]), "no value of factor"),
        (dict(constraints=[Constraint(metric="x", kind="output", maximum=3)]), "names a factor"),
        (dict(constraints=[Constraint(metric="y", kind="input", maximum=3)]), "unknown factor"),
        (dict(design_method="magic"), "unsupported design method"),
        (
            dict(
                measurement_definitions=[MeasurementDefinition(metric="y", unit="u"), MeasurementDefinition(metric="y", unit="u")]
            ),
            "unique",
        ),
        (
            dict(
                factors=[
                    Factor(name="x", factor_type="discrete", values=[1]),
                    Factor(name="x", factor_type="discrete", values=[2]),
                ]
            ),
            "unique",
        ),
        (dict(factors=[Factor(name="y", factor_type="discrete", values=[1])]), "both a factor and a metric"),
        (
            dict(objectives=[Objective(metric="y", direction="minimize"), Objective(metric="y", direction="minimize")]),
            "duplicate",
        ),
    ],
)
def test_spec_validation_rejects_inconsistent_definitions(kwargs, match):
    with pytest.raises(ValueError, match=match):
        spec(**kwargs)


def test_block_factors_are_separate_from_treatments():
    s = spec(
        factors=[
            Factor(name="x", factor_type="continuous", lower=0, upper=1),
            Factor(name="day", factor_type="categorical", values=["mon", "tue"], role="block"),
        ]
    )
    assert [f.name for f in s.treatment_factors] == ["x"] and [f.name for f in s.block_factors] == ["day"]
    with pytest.raises(ValueError, match="treatment factor"):
        spec(factors=[Factor(name="day", factor_type="categorical", values=["a", "b"], role="block")])


def test_derived_metrics_validated_and_unsafe_expressions_rejected():
    s = spec(derived_metrics=[DerivedMetric(name="ratio", unit="1", expression="y / z")])
    assert "ratio" in s.metric_names
    for bad in ("__import__('os').system('x')", "y.real", "y[0]", "lambda: 1", "y if z else 1", "open('f')"):
        with pytest.raises(ValueError):
            DerivedMetric(name="d", unit="u", expression=bad)
    with pytest.raises(ValueError, match="undefined metrics"):
        spec(derived_metrics=[DerivedMetric(name="d", unit="u", expression="y + nothing")])
    with pytest.raises(ValueError):
        DerivedMetric(name="d", unit="u")  # needs expression or function


def test_derived_metric_can_be_an_objective():
    s = spec(
        derived_metrics=[DerivedMetric(name="eff", unit="1", expression="y / z")],
        objectives=[Objective(metric="eff", direction="maximize")],
    )
    assert s.objectives[0].metric == "eff"


def test_unknown_fields_rejected_and_execution_policy_bounds():
    with pytest.raises(ValueError):
        spec(unexpected=1)
    with pytest.raises(ValueError):
        ExecutionPolicy(max_concurrency=0)
    assert spec(execution=ExecutionPolicy(max_retries=2)).execution.max_retries == 2


def test_run_lifecycle_transitions_enforced_and_recorded():
    run = Run(experiment_id="e", configuration={}, order=0, status=RunStatus.CREATED)
    run.transition(RunStatus.QUEUED)
    run.transition(RunStatus.RUNNING)
    run.transition(RunStatus.COMPLETED, reason="ok")
    assert [h["to"] for h in run.history] == ["queued", "running", "completed"]
    with pytest.raises(InvalidTransitionError):
        run.transition(RunStatus.RUNNING)  # completed is terminal
    failed = Run(experiment_id="e", configuration={}, order=0, status=RunStatus.RUNNING)
    failed.transition(RunStatus.FAILED)
    with pytest.raises(InvalidTransitionError):
        failed.transition(RunStatus.COMPLETED)  # failed runs cannot become completed
    failed.transition(RunStatus.QUEUED)  # only explicit retry path


def test_transition_tables_cover_every_state():
    assert set(RUN_TRANSITIONS) == set(RunStatus) and set(EXPERIMENT_TRANSITIONS) == set(ExperimentStatus)
    assert [s.value for s in ExperimentStatus] == [
        "created",
        "validated",
        "designed",
        "queued",
        "running",
        "waiting_for_measurements",
        "completed",
        "failed",
        "cancelled",
    ]
