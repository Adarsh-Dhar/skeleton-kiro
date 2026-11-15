"""Quickstart: define → register → design → execute → analyse → optimise → report, in ~40 lines.

Run from the repository root:  python -m examples.quickstart
"""

import asyncio

from examples.third_domain_adapter.adapter import CoffeeRoastAdapter
from trialforge import (
    AdapterRegistry,
    Analyzer,
    ExperimentService,
    ExperimentSpec,
    Factor,
    MeasurementDefinition,
    Objective,
    Optimizer,
)


async def main() -> None:
    spec = ExperimentSpec(
        experiment_id="quickstart-roast",  # fixed id => identical results on every run
        name="Roast study",
        hypothesis="Higher temperature with moderate airflow improves cup quality.",
        factors=[
            Factor(name="temperature", factor_type="continuous", lower=195, upper=215, unit="degC"),
            Factor(name="duration", factor_type="continuous", lower=6, upper=14, unit="min"),
            Factor(name="airflow", factor_type="continuous", lower=0, upper=100, unit="percent"),
        ],
        objectives=[Objective(metric="quality", direction="maximize")],
        measurement_definitions=[
            MeasurementDefinition(metric="quality", unit="score"),
            MeasurementDefinition(metric="moisture", unit="percent"),
            MeasurementDefinition(metric="colour", unit="index"),
        ],
        design_method="central_composite",
        design_options={"center_points": 3},
        run_budget=40,
        random_seed=7,
    )
    registry = AdapterRegistry()
    registry.register(CoffeeRoastAdapter())  # any object implementing the adapter protocol
    service = ExperimentService(registry=registry)  # in-memory SQLite; pass a repository for durability

    result = await service.run(spec)  # design + execute (persisted automatically)
    analysis = Analyzer().analyse(result)
    best = next(r for r in result.runs if r.run_id == analysis.best_run_id)
    print(f"{len(result.runs)} runs, status={result.status.value}")
    print("best observed:", {k: round(v, 1) for k, v in best.configuration.items()})
    quad = analysis.regression[0]
    print(
        f"regression R²={quad.r_squared:.3f}; significant terms:",
        [c.term for c in quad.coefficients if c.p_value is not None and c.p_value < 0.05 and c.term != "Intercept"],
    )

    recs = Optimizer().suggest(spec, result, count=2, method="bayesian")
    for r in recs:
        cfg = {k: round(v, 1) for k, v in r.configuration.items()}
        print(f"next: {cfg} kind={r.prediction_kind} EI={r.expected_improvement:.3f}")

    final, stop = await service.run_sequential(spec.experiment_id, iterations=3, batch_size=2)
    print(f"after sequential optimisation: {len(final.runs)} runs, stop reason: {stop.reason}")
    report = service.report(final)
    print(f"report v{report.report_version}; best config:", {k: round(v, 1) for k, v in report.best_configuration.items()})


if __name__ == "__main__":
    asyncio.run(main())
