import asyncio

import pytest

from trialforge import (
    AdapterCapabilities,
    AdapterMetadata,
    Constraint,
    ExecutionResponse,
    ExperimentService,
    ExperimentSpec,
    Factor,
    Measurement,
    MeasurementDefinition,
    Objective,
)
from trialforge.designer.engine import DesignError, ExperimentDesigner


class ImmediateAdapter:
    metadata = AdapterMetadata("constraints")
    capabilities = AdapterCapabilities(immediate_execution=True)

    async def validate(self, config):
        return None

    async def prepare(self, context):
        return None

    async def execute(self, config):
        return ExecutionResponse(completed=True, measurements=[Measurement(run_id="unused", metric="score", value=1, unit="u")])

    async def collect_result(self, run_id):
        raise AssertionError("immediate adapter should not collect")

    async def cancel(self, run_id):
        return None


def make_spec(*, method="factorial", budget=4, constraint=None):
    return ExperimentSpec(
        name="constrained",
        factors=[
            Factor(name="x", factor_type="discrete", values=[0, 1]),
            Factor(name="y", factor_type="discrete", values=[0, 1]),
        ],
        objectives=[Objective(metric="score", direction="maximize")],
        measurement_definitions=[MeasurementDefinition(metric="score", unit="u")],
        constraints=[constraint] if constraint else [],
        run_budget=budget,
        design_method=method,
        random_seed=13,
    )


def test_factorial_design_filters_combinations_and_rejects_budget_shortfall():
    spec = make_spec(constraint=Constraint(metric="x", equals=1))
    plan = ExperimentDesigner().design(spec)
    assert len(plan.runs) == 2
    assert all(run.configuration["x"] == 1 for run in plan.runs)
    assert all(spec.constraints[0].accepts(run.configuration["x"]) for run in plan.runs)
    with pytest.raises(DesignError, match="exceeds budget"):
        ExperimentDesigner().design(make_spec(budget=1, constraint=Constraint(metric="x", equals=1)))


def test_random_design_satisfies_constraints_and_budget():
    spec = make_spec(method="random", budget=6, constraint=Constraint(metric="x", equals=1))
    plan = ExperimentDesigner().design(spec)
    assert len(plan.runs) == 6
    assert all(run.configuration["x"] == 1 for run in plan.runs)


def test_service_prevalidates_plan_configuration_before_adapter_calls():
    class CountingAdapter(ImmediateAdapter):
        def __init__(self):
            self.calls = 0

        async def validate(self, config):
            self.calls += 1

    async def execute_invalid_plan():
        adapter = CountingAdapter()
        service = ExperimentService(adapter)
        spec = make_spec()
        plan = ExperimentDesigner().design(spec)
        plan.runs[0].configuration["x"] = 99
        with pytest.raises(ValueError, match="invalid value"):
            await service.execute(plan, spec)
        assert adapter.calls == 0

    asyncio.run(execute_invalid_plan())
