# Adapter development guide

An adapter connects TRIALFORGE to *your* system (simulator, instrument, meter, rig, API). You write it in **your own package**
and register it explicitly. No TRIALFORGE source changes are needed, and the core never imports your code.

## The contract

```python
from trialforge import (
    AdapterCapabilities,
    AdapterMetadata,
    ExecutionContext,
    ExecutionResponse,
    MeasurementResponse,
    ExecutionStatusResponse,
    Measurement,
    AdapterError,
)


class MyAdapter:
    metadata = AdapterMetadata(
        name="my-system",
        version="1.0",
        description="...",
        domain="...",
        synthetic=False,  # True for simulated data: reports will say so
        input_schema={"x": {"type": "continuous", "min": 0, "max": 10}},
    )
    capabilities = AdapterCapabilities(
        immediate_execution=True,
        delayed_measurements=False,
        cancellation=False,
        time_series=False,
        concurrent=True,
        max_concurrency=4,
        retryable=True,
        idempotent=False,
        resumable=False,
    )

    # required ---------------------------------------------------------------------------------
    async def validate(self, config: dict) -> None: ...  # raise AdapterError if config is not runnable
    async def prepare(self, context: ExecutionContext) -> None: ...
    async def execute(self, config: dict) -> ExecutionResponse: ...
    async def collect_result(self, run_id: str) -> MeasurementResponse: ...
    async def cancel(self, run_id: str) -> None: ...

    # optional ---------------------------------------------------------------------------------
    async def execute_run(self, ctx: ExecutionContext) -> ExecutionResponse: ...  # preferred: concurrency-safe
    async def status(self, handle: str) -> ExecutionStatusResponse: ...  # restart recovery
    async def available(self) -> bool: ...
    async def startup(self) -> None: ...
    async def shutdown(self) -> None: ...
```

`isinstance(adapter, ExperimentAdapter)` checks that the required members exist (it is a runtime-checkable protocol).

### `execute_run` vs `prepare` + `execute`

If `execute_run(context)` exists the engine calls it (concurrently, as allowed by capabilities). It receives everything: `run_id`,
`configuration`, `attempt`, `idempotency_key`, `random_seed`, and `metadata["requested_metrics"]` (the metrics the spec defines; emit
only those). If you implement only `prepare` + `execute`, the engine **serialises** the pair per adapter because they share your state,
and `execute` must put a placeholder run id (`""`, `"unused"` or `"unbound"`) on its measurements; the engine binds the right id.

### Responses

* Immediate: `ExecutionResponse(completed=True, measurements=[...])`.
* Delayed: `ExecutionResponse(completed=False, handle="...")` (declare `delayed_measurements=True`). The run becomes
  `WAITING_FOR_MEASUREMENTS`; later `service.collect_pending(experiment_id)` calls `collect_result(run_id)`, which returns
  `MeasurementResponse(completed=False)` until data exist.
* Raise on failure. Unexpected exceptions fail the run (after retries if the adapter is `retryable`). `AdapterError` is the
  conventional type; raise it from `validate` for configurations you cannot run (such rejections are never retried).

### Measurements

`Measurement(run_id, metric, value, unit, timestamp=..., source="instrument-1", channel=None, quality_flags=[], metadata={})`.

* Use the units you have; the engine converts to the unit in the `MeasurementDefinition` when they are compatible (e.g. `ms`→`s`,
  `W`→`kW`, `degF`→`degC`) and flags incompatible ones as `unit_mismatch` (unusable).
* Set `metadata["synthetic"] = True` for simulated data and put the origin in `source` (provenance).
* Time series: one `Measurement` per sample with a `timestamp`, optional `channel`, and `metadata["sample_index"]` (see `make_series`). Define
  the metric with `time_series=True` and declare the `time_series` capability.
* Sensor problems: set `metadata["sensor_warning"] = True` to keep a value but mark it `sensor_warning`.
* Do not delete or "fix" suspicious values: the engine flags outliers; analysis decides what to use.

### Capabilities drive engine behaviour

See [execution & recovery](../architecture/execution_and_recovery.md). Be honest: `idempotent=True` means executing the same run id twice
is safe and gives the same effect; `resumable=True` means `status()` / `collect_result()` work after a process restart. Wrong claims
turn into duplicated or lost work after a crash.

## Registering and using an adapter

```python
# docs-test
import asyncio
from trialforge import (
    AdapterCapabilities,
    AdapterMetadata,
    AdapterRegistry,
    ExecutionResponse,
    ExperimentService,
    ExperimentSpec,
    Factor,
    Measurement,
    MeasurementDefinition,
    MeasurementResponse,
    Objective,
)


class GreenhouseAdapter:
    metadata = AdapterMetadata(name="greenhouse", version="0.1", description="tomato yield (toy model)", synthetic=True)
    capabilities = AdapterCapabilities(concurrent=True, max_concurrency=4, idempotent=True)

    async def validate(self, config): ...
    async def prepare(self, context): ...
    async def execute(self, config):
        return await self._run("unused", config)

    async def execute_run(self, ctx):
        return await self._run(ctx.run_id, ctx.configuration)

    async def _run(self, run_id, config):
        kg = 5 - (config["humidity"] - 70) ** 2 / 100
        return ExecutionResponse(
            completed=True,
            measurements=[Measurement(run_id=run_id, metric="yield_kg", value=kg, unit="kg", metadata={"synthetic": True})],
        )

    async def collect_result(self, run_id):
        return MeasurementResponse(completed=False)

    async def cancel(self, run_id): ...


spec = ExperimentSpec(
    experiment_id="greenhouse-doc",
    name="Greenhouse",
    adapter_name="greenhouse",  # selects the adapter by name
    factors=[Factor(name="humidity", factor_type="continuous", lower=40, upper=90)],
    objectives=[Objective(metric="yield_kg", direction="maximize")],
    measurement_definitions=[MeasurementDefinition(metric="yield_kg", unit="kg")],
    design_method="random",
    run_budget=12,
    random_seed=3,
)


async def main():
    registry = AdapterRegistry()
    registry.register(GreenhouseAdapter())  # several adapters may be registered side by side
    service = ExperimentService(registry=registry)
    await service.run(spec)
    result, stop = await service.run_sequential(spec.experiment_id, iterations=1, batch_size=2)
    best = max(result.runs, key=lambda r: r.processed_measurements[0].value)
    print(len(result.runs), "runs; best humidity", round(best.configuration["humidity"], 1))


asyncio.run(main())
```

A complete delayed-measurement adapter is in [`examples/custom_adapter_tutorial.py`](../../examples/custom_adapter_tutorial.py).

## Extension points (plug-ins)

Plug-ins are plain callables registered **by name** in a `PluginRegistry`; specs refer to the name. Nothing is imported or executed
from strings.

| Kind | Contract | Referenced from |
|---|---|---|
| `metric` | `fn(outcomes: dict[str, float]) -> float` | `DerivedMetric(name=..., function="name")` |
| `transformation` | `fn(values: list[float], **params) -> list[float]` | `MeasurementDefinition(transformations=[{"name": "n", "params": {...}}])` |
| `objective` | `fn(outcomes) -> float` | `Objective(metric="label", function="name")` (oriented by `direction`) |
| `constraint` | `fn(configuration, outcomes) -> bool` | `Constraint(kind="input"/"output", function="name")` |
| `optimizer` | `fn(OptimizerContext) -> list[Recommendation]` | `Optimizer.suggest(..., method="name")` |
| `report` | `fn(report, result) -> dict` | `ReportEngine(extensions=["name"])` (explicit opt-in) |

```python
from trialforge import ExperimentService, PluginRegistry

plugins = PluginRegistry()  # or use the process-wide DEFAULT_PLUGINS
plugins.register("transformation", "clip", lambda vals, lo, hi: [min(max(v, lo), hi) for v in vals])
service = ExperimentService(adapter, plugins=plugins)
```

Duplicate registration raises (`replace=True` to override); unknown kinds, non-callables and unregistered references are rejected.
Custom units: `UnitRegistry.register("money", "cent", 0.01)`.

## Testing your adapter

Copy the contract suite in `packages/trialforge-core/tests/contracts/test_adapter_contract.py` and add a case for your adapter. It checks
protocol conformance, metadata, validation, standard measurements and units, determinism/idempotency, concurrency safety, the legacy
path, a full experiment through the service, and schema round-tripping.

## Checklist

- [ ] Metadata and honest capabilities (`synthetic=True` for simulations)
- [ ] `validate` rejects what you cannot run; configuration domains also enforced by the spec
- [ ] Measurements carry `unit`, `source`, correct `metric` names, and `synthetic` metadata where applicable
- [ ] `execute_run` is safe to call concurrently (or declare `concurrent=False`)
- [ ] Deterministic given (run id, configuration) if you claim `idempotent`
- [ ] Contract tests pass
