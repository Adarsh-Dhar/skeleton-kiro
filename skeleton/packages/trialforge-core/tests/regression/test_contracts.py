import asyncio

from examples.f1_simulator_adapter.adapter import SyntheticF1Adapter
from examples.wattwise_meter_adapter.adapter import SyntheticWattwiseAdapter

from trialforge import ExperimentService, ExperimentSpec, Factor, MeasurementDefinition, Objective


def test_demo_domain_adapters_satisfy_shared_contract():
    async def execute(adapter, factors, definitions, objectives):
        spec = ExperimentSpec(
            name="adapter contract",
            factors=factors,
            objectives=objectives,
            measurement_definitions=definitions,
            run_budget=1,
            random_seed=3,
        )
        service = ExperimentService(adapter)
        result = await service.run(spec, method="random")
        assert result.runs[0].status.value == "completed"
        assert result.runs[0].measurements

    async def work():
        await execute(
            SyntheticF1Adapter(),
            [
                Factor(name="front_wing", factor_type="discrete", values=[20]),
                Factor(name="rear_wing", factor_type="discrete", values=[24]),
                Factor(name="suspension", factor_type="discrete", values=[5]),
            ],
            [MeasurementDefinition(metric="lap_time", unit="seconds"), MeasurementDefinition(metric="stability", unit="score")],
            [Objective(metric="lap_time", direction="minimize")],
        )
        await execute(
            SyntheticWattwiseAdapter(),
            [
                Factor(name="charge_start_hour", factor_type="discrete", values=[6]),
                Factor(name="charge_power_kw", factor_type="discrete", values=[7]),
                Factor(name="cooling_setpoint", factor_type="discrete", values=[24]),
            ],
            [
                MeasurementDefinition(metric="energy_cost", unit="currency"),
                MeasurementDefinition(metric="energy_kwh", unit="kWh"),
            ],
            [Objective(metric="energy_cost", direction="minimize")],
        )

    asyncio.run(work())
