"""WATTWISE LAB demo on SIMULATED meter data: when should the EV charge?

Run:  python -m examples.wattwise_meter_adapter.run_example
"""

import asyncio

from examples.wattwise_meter_adapter.adapter import SyntheticWattwiseAdapter, Tariff, measurements_from_csv
from trialforge import (
    Constraint,
    ExperimentService,
    ExperimentSpec,
    Factor,
    MeasurementDefinition,
    Objective,
)


async def main() -> None:
    spec = ExperimentSpec(
        experiment_id="demo-wattwise-charging",  # fixed id => identical results on every run
        name="Charging schedule (simulated meter)",
        factors=[
            Factor(name="charge_start_hour", factor_type="discrete", values=[18, 22, 0, 2, 4, 6]),
            Factor(name="charge_power_kw", factor_type="discrete", values=[3.7, 7.4, 11.0]),
            Factor(name="cooling_setpoint", factor_type="discrete", values=[24]),
        ],
        objectives=[Objective(metric="energy_cost", direction="minimize")],
        constraints=[Constraint(metric="departure_charge_pct", minimum=80)],  # must be ≥80% charged at departure
        measurement_definitions=[
            MeasurementDefinition(metric="energy_cost", unit="currency"),
            MeasurementDefinition(metric="energy_kwh", unit="kWh"),
            MeasurementDefinition(metric="peak_power_kw", unit="kW"),
            MeasurementDefinition(metric="departure_charge_pct", unit="percent"),
        ],
        design_options={"replicates": 2},
        run_budget=60,
        random_seed=3,
    )
    service = ExperimentService(SyntheticWattwiseAdapter())
    result = await service.run(spec)
    analysis = service.analyse(result)
    best = next(r for r in result.runs if r.run_id == analysis.best_run_id)
    print(f"{len(result.runs)} runs, {len(analysis.infeasible_runs)} violate the departure-charge constraint")
    print("cheapest FEASIBLE schedule:", best.configuration)
    cheapest_bad = min(analysis.infeasible_runs, key=lambda r: r["objectives"]["energy_cost"])
    why = cheapest_bad["violations"][0]
    print(
        f"cheapest REJECTED schedule: {cheapest_bad['configuration']} costs {cheapest_bad['objectives']['energy_cost']:.2f} "
        f"but departure charge is only {why['value']:.0f}% (needs >=80%)"
    )

    # Import a real-looking meter export (W readings) and price it with a custom tariff.
    rows = ["timestamp,power"] + [f"2026-02-01T{h:02d}:00:00+00:00,{w}" for h, w in enumerate([800, 800, 3400, 3400, 600, 600])]
    ms = measurements_from_csv("run-x", "\n".join(rows), tariff=Tariff(periods=((0, 24, 0.15),)), unit="W")
    print("CSV import:", {m.metric: round(m.value, 3) for m in ms if m.metric != "grid_power"}, "(reading_kind=imported)")
    print("NOTE: simulated readings are labelled synthetic; imported readings are not.")


if __name__ == "__main__":
    asyncio.run(main())
