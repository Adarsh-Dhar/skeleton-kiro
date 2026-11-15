"""Coffee roasting: a complete experiment in a domain the core knows nothing about.

Run:  python -m examples.third_domain_adapter.run_example
"""

import asyncio

from examples.third_domain_adapter.adapter import CoffeeRoastAdapter
from trialforge import (
    Constraint,
    ExperimentService,
    ExperimentSpec,
    Factor,
    MeasurementDefinition,
    Objective,
    ReportEngine,
)


async def main() -> None:
    spec = ExperimentSpec(
        experiment_id="demo-roast-moisture",  # fixed id => identical results on every run
        name="Roast: quality with a moisture limit",
        factors=[
            Factor(name="temperature", factor_type="continuous", lower=195, upper=215, unit="degC"),
            Factor(name="duration", factor_type="continuous", lower=6, upper=14, unit="min"),
            Factor(name="airflow", factor_type="discrete", values=[30, 50]),
        ],
        objectives=[Objective(metric="quality", direction="maximize")],
        constraints=[Constraint(metric="moisture", minimum=1.5)],  # OUTPUT constraint: judged after measurement
        measurement_definitions=[
            MeasurementDefinition(metric="quality", unit="score"),
            MeasurementDefinition(metric="moisture", unit="percent"),
            MeasurementDefinition(metric="colour", unit="index"),
        ],
        design_options={"replicates": 2},
        run_budget=40,
        random_seed=1,
    )
    service = ExperimentService(CoffeeRoastAdapter())
    result = await service.run(spec)
    analysis = service.analyse(result)
    print(f"{len(result.runs)} runs; {len(analysis.feasible_run_ids)} feasible")
    best = next(r for r in result.runs if r.run_id == analysis.best_run_id)
    print("best feasible setup:", best.configuration)
    top = max(analysis.main_effects, key=lambda e: abs(e.mean_difference or 0))
    print(
        f"largest marginal effect: {top.factor} {top.control_level}→{top.level}: "
        f"{top.mean_difference:+.2f} (evidence={top.evidence})"
    )
    print("interaction temperature×duration:", round(analysis.interactions[0].estimate, 2))
    print(ReportEngine.to_csv(result).splitlines()[0])


if __name__ == "__main__":
    asyncio.run(main())
