import asyncio

from trialforge import (
    AdapterCapabilities,
    AdapterMetadata,
    ExecutionContext,
    ExecutionResponse,
    ExperimentService,
    ExperimentSpec,
    Factor,
    Measurement,
    MeasurementDefinition,
    MeasurementResponse,
    Objective,
    RunStatus,
)


class DelayedAdapter:
    metadata = AdapterMetadata("delayed")
    capabilities = AdapterCapabilities(immediate_execution=False, delayed_measurements=True, cancellation=True)

    def __init__(self):
        self.pending = {}
        self.current = None

    async def validate(self, config):
        return None

    async def prepare(self, context: ExecutionContext):
        self.current = context.run_id

    async def execute(self, config):
        self.pending[self.current] = config["level"]
        return ExecutionResponse(completed=False, handle=f"job-{self.current}")

    async def collect_result(self, run_id):
        if run_id not in self.pending:
            return MeasurementResponse(completed=False)
        value = self.pending.pop(run_id)
        return MeasurementResponse(
            completed=True, measurements=[Measurement(run_id=run_id, metric="outcome", value=value, unit="u")]
        )

    async def cancel(self, run_id):
        self.pending.pop(run_id, None)


def spec():
    return ExperimentSpec(
        name="Delayed",
        factors=[Factor(name="level", factor_type="discrete", values=[1, 2])],
        objectives=[Objective(metric="outcome", direction="maximize")],
        measurement_definitions=[MeasurementDefinition(metric="outcome", unit="u")],
        run_budget=2,
        random_seed=4,
    )


def test_delayed_run_collection_and_analysis_input_optimization():
    async def work():
        service = ExperimentService(DelayedAdapter())
        experiment = spec()
        result = await service.run(experiment, method="factorial")
        assert result.status.value == "waiting_for_measurements"
        assert all(run.status == RunStatus.WAITING_FOR_MEASUREMENTS for run in result.runs)
        collected = await service.collect_pending(experiment.experiment_id)
        assert collected.status.value == "completed"
        assert all(run.status == RunStatus.COMPLETED for run in collected.runs)
        analysis = service.analyse(collected)
        assert analysis.descriptive["outcome"]["mean"] == 1.5
        # Exhausted run budget correctly stops sequential suggestions.
        assert service.optimise(experiment, analysis) == []

    asyncio.run(work())


def test_adapter_failure_is_not_promoted_to_success():
    class BrokenAdapter(DelayedAdapter):
        async def validate(self, config):
            raise RuntimeError("configuration rejected")

    async def work():
        service = ExperimentService(BrokenAdapter())
        result = await service.run(spec(), method="factorial")
        assert result.status.value == "failed"
        assert all(run.status == RunStatus.FAILED for run in result.runs)
        assert all("configuration rejected" in run.error for run in result.runs)

    asyncio.run(work())
