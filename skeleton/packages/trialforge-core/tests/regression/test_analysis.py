from trialforge import (
    AnalysisEngine,
    DesignPlan,
    ExperimentResult,
    ExperimentSpec,
    Factor,
    Measurement,
    MeasurementDefinition,
    Objective,
    Run,
    RunStatus,
)


def test_aggregated_measurements_drive_known_descriptives():
    spec = ExperimentSpec(
        name="known",
        factors=[Factor(name="level", factor_type="discrete", values=[0, 1])],
        objectives=[Objective(metric="response", direction="minimize")],
        measurement_definitions=[MeasurementDefinition(metric="response", unit="u", aggregation="mean")],
        run_budget=4,
        random_seed=1,
    )
    configurations = [{"level": 0}, {"level": 0}, {"level": 1}, {"level": 1}]
    runs = []
    for index, (config, samples) in enumerate(zip(configurations, ([1, 3], [3, 5], [4, 6], [8, 10]))):
        run_id = f"r{index}"
        runs.append(
            Run(
                run_id=run_id,
                experiment_id=spec.experiment_id,
                configuration=config,
                order=index,
                status=RunStatus.COMPLETED,
                measurements=[Measurement(run_id=run_id, metric="response", value=value, unit="u") for value in samples],
            )
        )
    plan = DesignPlan(
        experiment_id=spec.experiment_id, method="ofat", runs=runs, control_configuration={"level": 0}, random_seed=1
    )
    result = ExperimentResult(spec=spec, plan=plan, runs=runs, status="completed")
    analyzed = AnalysisEngine().analyse(result)
    summary = analyzed.descriptive["response"]
    assert summary["count"] == 4
    assert summary["mean"] == 5
    assert summary["median"] == 4.5
    assert summary["variance"] == 26 / 3
    effect = analyzed.effects[0]
    assert effect.control_mean == 3
    assert effect.treatment_mean == 7
    assert effect.mean_difference == 4
    assert effect.sample_size_control == 2
    assert effect.confidence_interval_95 is not None
