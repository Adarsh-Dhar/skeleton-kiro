"""Tutorial adapter: a *delayed-measurement* adapter written outside the package (see docs/adapter_development).

Run:  python -m examples.custom_adapter_tutorial
"""

import asyncio

from trialforge import (
    AdapterCapabilities,
    AdapterError,
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
    PluginRegistry,
)


class OvenAdapter:
    """Pretends to be a slow oven: execute() returns a handle, results appear later via collect_result()."""

    metadata = AdapterMetadata(name="oven", version="1.0", description="Slow oven (tutorial)", synthetic=True)
    capabilities = AdapterCapabilities(immediate_execution=False, delayed_measurements=True, resumable=True)

    def __init__(self) -> None:
        self._done: dict[str, float] = {}

    async def validate(self, config):
        if not 100 <= config["temp"] <= 250:
            raise AdapterError("temp must be within 100-250")

    async def prepare(self, context: ExecutionContext) -> None:
        return None

    async def execute_run(self, ctx: ExecutionContext) -> ExecutionResponse:
        self._done[ctx.run_id] = 100 - abs(ctx.configuration["temp"] - 180) / 2  # 'baked' result, ready later
        return ExecutionResponse(completed=False, handle=f"oven-{ctx.run_id}")

    async def execute(self, config):  # legacy path, unused by the engine because execute_run exists
        raise AdapterError("use execute_run")

    async def collect_result(self, run_id: str) -> MeasurementResponse:
        if run_id not in self._done:
            return MeasurementResponse(completed=False)
        m = Measurement(run_id=run_id, metric="doneness", value=self._done[run_id], unit="score", source="oven-sim")
        return MeasurementResponse(completed=True, measurements=[m])

    async def cancel(self, run_id: str) -> None:
        self._done.pop(run_id, None)


async def main() -> None:
    plugins = PluginRegistry()
    plugins.register("transformation", "round1", lambda vals: [round(v, 1) for v in vals])  # explicit plug-in registration
    spec = ExperimentSpec(
        experiment_id="tutorial-oven",  # fixed id => identical results on every run
        name="Oven",
        factors=[Factor(name="temp", factor_type="discrete", values=[150, 180, 210])],
        objectives=[Objective(metric="doneness", direction="maximize")],
        measurement_definitions=[MeasurementDefinition(metric="doneness", unit="score", transformations=[{"name": "round1"}])],
        run_budget=5,
    )
    service = ExperimentService(OvenAdapter(), plugins=plugins)
    result = await service.run(spec)
    print("after execute:", result.status.value, [r.status.value for r in result.runs])
    result = await service.collect_pending(spec.experiment_id)
    print("after collect:", result.status.value)
    analysis = service.analyse(result)
    print("best:", next(r.configuration for r in result.runs if r.run_id == analysis.best_run_id))


if __name__ == "__main__":
    asyncio.run(main())
