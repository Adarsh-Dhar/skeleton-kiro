import asyncio

import pytest
from examples.third_domain_adapter.adapter import CoffeeRoastAdapter

from trialforge import (
    Constraint,
    ExperimentService,
    ExperimentSpec,
    Factor,
    MeasurementDefinition,
    Objective,
    SQLiteExperimentRepository,
)


def make_spec(seed=9):
    return ExperimentSpec(
        name="Synthetic roasting experiment",
        factors=[
            Factor(name="temperature", factor_type="continuous", lower=195, upper=215),
            Factor(name="duration", factor_type="discrete", values=[8, 10, 12]),
            Factor(name="airflow", factor_type="discrete", values=[30, 50]),
        ],
        objectives=[Objective(metric="quality", direction="maximize")],
        constraints=[Constraint(metric="airflow", minimum=0, maximum=100)],
        measurement_definitions=[
            MeasurementDefinition(metric="quality", unit="score"),
            MeasurementDefinition(metric="moisture", unit="percent"),
            MeasurementDefinition(metric="colour", unit="index"),
        ],
        run_budget=20,
        random_seed=seed,
    )


def test_design_reproducible_and_unique():
    adapter = CoffeeRoastAdapter()
    service = ExperimentService(adapter)
    spec = make_spec()
    plan1 = service.design(spec)
    plan2 = service.design(spec)
    assert [r.configuration for r in plan1.runs] == [r.configuration for r in plan2.runs]
    assert len({r.run_id for r in plan1.runs}) == 18
    assert [r.run_id for r in plan1.runs] == [r.run_id for r in plan2.runs]
    assert len(plan1.runs) == 18


def test_end_to_end_analysis_optimization_report_and_sqlite(tmp_path):
    async def work():
        spec = make_spec()
        service = ExperimentService(CoffeeRoastAdapter())
        result = await service.run(spec)
        assert result.status.value == "completed"
        assert all(run.measurements for run in result.runs)
        analysis = service.analyse(result)
        assert analysis.descriptive["quality"]["count"] == 18
        recommendations = service.optimise(spec, result)
        assert recommendations
        report = service.report(result, analysis)
        assert report.completed_runs == 18
        assert report.best_configuration is not None
        repo = SQLiteExperimentRepository(f"sqlite:///{tmp_path / 'runs.db'}")
        repo.save(result)
        restored = repo.get(spec.experiment_id)
        assert len(restored.runs) == 18
        assert restored.runs[0].measurements[0].value == result.runs[0].measurements[0].value

    asyncio.run(work())


def test_invalid_model_contracts():
    with pytest.raises(ValueError):
        Factor(name="x", factor_type="continuous", lower=5, upper=1)
    with pytest.raises(ValueError):
        ExperimentSpec(
            name="bad",
            factors=[Factor(name="x", factor_type="discrete", values=[1])],
            objectives=[Objective(metric="missing", direction="minimize")],
            measurement_definitions=[MeasurementDefinition(metric="known", unit="u")],
            run_budget=2,
        )


def test_exceeds_factorial_budget_fails_explicitly():
    service = ExperimentService(CoffeeRoastAdapter())
    spec = make_spec().model_copy(update={"run_budget": 3})
    with pytest.raises(ValueError, match="exceeds budget"):
        service.design(spec)
