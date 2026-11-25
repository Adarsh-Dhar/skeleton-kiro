"""Coffee roasting: a complete experiment in a domain the core knows nothing about.

The roaster is a SYNTHETIC simulator (see adapter.py). No coffee-roasting measurement here is real.

The example registers the adapter explicitly in an ``AdapterRegistry`` and lets ``ExperimentSpec.adapter_name`` select it, then
runs design, execution, analysis, optimisation and reporting. The SDK is not changed for this domain.

Run:  python -m examples.third_domain_adapter.run_example
"""
import asyncio

from examples.third_domain_adapter.adapter import CoffeeRoastAdapter
from trialforge import (
    AdapterRegistry,
    Constraint,
    ExperimentService,
    ExperimentSpec,
    Factor,
    MeasurementDefinition,
    Objective,
)


def build_spec() -> ExperimentSpec:
    return ExperimentSpec(
        experiment_id="demo-roast-moisture",  # fixed id => identical results on every run
        name="Roast: quality with a moisture limit",
        adapter_name="coffee-roast",  # selects the registered adapter by name
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


async def main() -> None:
    print("NOTE: every coffee-roasting measurement below is SYNTHETIC (invented response surface, not real roasting data).")

    # 1. Explicit registration. Nothing is discovered or imported implicitly.
    registry = AdapterRegistry()
    registry.register(CoffeeRoastAdapter())
    print("registered adapters:", registry.names())
    service = ExperimentService(registry=registry)

    # 2. Design, then execute through the adapter named by spec.adapter_name.
    spec = build_spec()
    plan = service.design(spec)
    print(f"design: {plan.method}, {len(plan.runs)} runs")
    result = await service.execute(plan, spec)
    print("adapters used:", sorted({r.adapter_name for r in result.runs if r.adapter_name}))

    # 3. Analysis.
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

    # 4. Optimisation: next configurations to try.
    recommendations = service.optimise(spec, result, count=2)
    for i, rec in enumerate(recommendations, 1):
        print(f"recommendation {i}: {rec.configuration} ({rec.rationale})")

    # 5. Report.
    report = service.report(result, analysis)
    print("report built:", type(report).__name__)
    from trialforge import ReportEngine

    print(ReportEngine.to_csv(result).splitlines()[0])


if __name__ == "__main__":
    asyncio.run(main())
