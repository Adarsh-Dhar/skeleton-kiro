# Quickstart

The nine steps of a typical application, using the coffee-roasting example adapter. Run from the repository root.

```python
# docs-test
import asyncio
from trialforge import (
    AdapterRegistry,
    Analyzer,
    Constraint,
    ExperimentService,
    ExperimentSpec,
    Factor,
    MeasurementDefinition,
    Objective,
    Optimizer,
    ReportEngine,
)
from examples.third_domain_adapter.adapter import CoffeeRoastAdapter

# 1-2. Define the experiment with the public models.
spec = ExperimentSpec(
    experiment_id="quickstart-doc",
    name="Roast study",
    hypothesis="Roast development drives quality",
    factors=[
        Factor(name="temperature", factor_type="continuous", lower=195, upper=215, unit="degC"),
        Factor(name="duration", factor_type="continuous", lower=6, upper=14, unit="min"),
        Factor(name="airflow", factor_type="discrete", values=[30, 50]),
    ],
    objectives=[Objective(metric="quality", direction="maximize")],
    constraints=[Constraint(metric="moisture", minimum=1.5)],  # an OUTPUT constraint (judged after measuring)
    measurement_definitions=[
        MeasurementDefinition(metric="quality", unit="score"),
        MeasurementDefinition(metric="moisture", unit="percent"),
        MeasurementDefinition(metric="colour", unit="index"),
    ],
    design_options={"replicates": 2},
    run_budget=40,
    random_seed=1,
)


async def main():
    # 3. Register an adapter (yours, or an example); no core code changes needed.
    registry = AdapterRegistry()
    registry.register(CoffeeRoastAdapter())
    service = ExperimentService(registry=registry)

    plan = service.design(spec)  # 4. reproducible design (persisted)
    print(plan.method, len(plan.runs), "runs; seed", plan.random_seed)
    result = await service.execute(plan, spec)  # 5-6. execute; measurements ingested, validated, converted, stored
    analysis = service.analyse(result)  # 7. statistics (persisted)
    best = next(r for r in result.runs if r.run_id == analysis.best_run_id)
    print("best feasible:", best.configuration)
    recs = service.optimise(spec, count=2)  # 8. next configurations (stored results are read from the database)
    print("next:", [r.strategy for r in recs])
    print(service.history(spec.experiment_id).configurations()[0]["status"])  # 9. history
    report = service.report(result, analysis)  #    report
    print(report.report_version, ReportEngine.to_json(report)[:40].replace("\n", " "))


asyncio.run(main())
```

## Concepts in one minute

* **Factors** are what you vary (`continuous`, `discrete`, `categorical`, or `role="block"` for nuisance variables).
* **Objectives** say what is better (`minimize`, `maximize`, `target`); several may be given, optionally weighted.
* **Constraints** are *input* (restrict configurations before running; e.g. `maximum` on a factor) or *output* (judged on measured
  results; e.g. `minimum` on a stability metric). `kind="auto"` decides from the name; use `kind=` to be explicit.
* An **adapter** runs one configuration and returns `Measurement`s. The engine converts units, flags quality problems, aggregates
  and persists everything with provenance.
* A **run** is the unit of analysis: its measurements are aggregated into one outcome per metric.

## Reproducibility

Designs are seeded by `ExperimentSpec.random_seed`. Run ids derive from `experiment_id`, so **set `experiment_id` explicitly** when
you want identical runs (and, for the example adapters, identical simulated noise) across processes; a spec created without one gets a
fresh UUID and therefore describes a new experiment.
