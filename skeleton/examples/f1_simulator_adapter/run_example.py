"""F1 SETUP LAB demo on SYNTHETIC data: fractional factorial screening, then Bayesian refinement.

Run:  python -m examples.f1_simulator_adapter.run_example
"""

import asyncio

from examples.f1_simulator_adapter.adapter import SyntheticF1Adapter, measurements_from_telemetry_csv
from trialforge import (
    Constraint,
    ExperimentService,
    ExperimentSpec,
    Factor,
    MeasurementDefinition,
    Objective,
)


def make_spec() -> ExperimentSpec:
    return ExperimentSpec(
        experiment_id="demo-f1-setup",  # fixed id => identical results on every run
        name="Setup lab (synthetic)",
        hypothesis="A rearward aero balance improves lap time while keeping the car stable.",
        factors=[
            Factor(name="front_wing", factor_type="continuous", lower=10, upper=40, unit="deg"),
            Factor(name="rear_wing", factor_type="continuous", lower=10, upper=40, unit="deg"),
            Factor(name="suspension", factor_type="continuous", lower=3, upper=9, unit="stiffness"),
            Factor(name="tyre_pressure_psi", factor_type="continuous", lower=19, upper=25, unit="psi"),
        ],
        objectives=[Objective(metric="lap_time", direction="minimize")],
        constraints=[Constraint(metric="stability", minimum=85)],  # require a stability score of at least 85
        measurement_definitions=[
            MeasurementDefinition(metric="lap_time", unit="seconds"),
            MeasurementDefinition(metric="stability", unit="score"),
            MeasurementDefinition(metric="sector_1", unit="seconds"),
            MeasurementDefinition(metric="sector_2", unit="seconds"),
            MeasurementDefinition(metric="sector_3", unit="seconds"),
        ],
        design_method="fractional_factorial",
        design_options={"generators": ["D=ABC"], "replicates": 2, "center_points": 2},  # 2^(4-1), resolution IV
        run_budget=40,
        random_seed=11,
    )


async def main() -> None:
    spec = make_spec()
    service = ExperimentService(SyntheticF1Adapter())
    result = await service.run(spec)
    info = result.plan.design_info
    print(
        f"screening: {len(result.runs)} runs, 2^({info['k']}-{info['p']}) design, "
        f"resolution {info['resolution']}, {info['defining_relation']}"
    )
    analysis = service.analyse(result)
    print(f"{len(analysis.feasible_run_ids)} feasible of {len(analysis.input_run_ids)}; infeasible runs are never 'best'")
    final, stop = await service.run_sequential(spec.experiment_id, iterations=3, batch_size=2, method="bayesian")
    analysis = service.analyse(final)
    best = next(r for r in final.runs if r.run_id == analysis.best_run_id)
    values = {p.metric: p.value for p in best.processed_measurements}
    print(
        f"refinement: {len(final.runs)} runs (stop: {stop.reason}); "
        f"best feasible lap {values['lap_time']:.3f}s, stability {values['stability']:.1f}"
    )
    print("best setup:", {k: round(v, 1) for k, v in best.configuration.items()})

    # Import recorded telemetry (here a tiny inline CSV standing in for a simulator export) for one extra lap.
    csv_text = "sector_1,sector_2,sector_3,stability\n24.1,33.2,23.0,88\n24.0,33.1,23.1,87"
    ms = measurements_from_telemetry_csv("run-x", csv_text)
    print("telemetry import:", {m.metric: round(m.value, 2) for m in ms if m.metadata["lap"] == 1}, "(synthetic=False)")
    print("DISCLAIMER: all simulated values are synthetic; no real F1 data is involved.")


if __name__ == "__main__":
    asyncio.run(main())
