# Tutorial: from design to report

We study EV charging with the WATTWISE example adapter (**simulated** meter): minimise electricity cost subject to ≥80 % charge at
departure. Run the finished script with `python -m examples.wattwise_meter_adapter.run_example`.

## 1. Define the experiment

```python
# docs-test
import asyncio
from trialforge import Constraint, ExperimentService, ExperimentSpec, Factor, MeasurementDefinition, Objective, ReportEngine
from examples.wattwise_meter_adapter.adapter import SyntheticWattwiseAdapter

spec = ExperimentSpec(
    experiment_id="tutorial-wattwise",
    name="Charging schedule",
    hypothesis="Charging after midnight is cheaper without missing departure",
    factors=[
        Factor(name="charge_start_hour", factor_type="discrete", values=[18, 22, 0, 2, 4, 6]),
        Factor(name="charge_power_kw", factor_type="discrete", values=[3.7, 7.4, 11.0]),
        Factor(name="cooling_setpoint", factor_type="discrete", values=[24]),
    ],
    objectives=[Objective(metric="energy_cost", direction="minimize")],
    constraints=[Constraint(metric="departure_charge_pct", minimum=80)],
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
```

`departure_charge_pct` names a metric, so the constraint is an **output** constraint. A constraint on `charge_start_hour` would be an
**input** constraint that removes configurations from the design (and the design says so: `design_info["complete"]` becomes False).

## 2. Design, run, analyse

```python
# docs-test
import asyncio
from trialforge import Constraint, ExperimentService, ExperimentSpec, Factor, MeasurementDefinition, Objective, ReportEngine
from examples.wattwise_meter_adapter.adapter import SyntheticWattwiseAdapter

spec = ExperimentSpec(
    experiment_id="tutorial-wattwise-2",
    name="Charging schedule",
    factors=[
        Factor(name="charge_start_hour", factor_type="discrete", values=[18, 22, 0, 2, 4, 6]),
        Factor(name="charge_power_kw", factor_type="discrete", values=[3.7, 7.4, 11.0]),
        Factor(name="cooling_setpoint", factor_type="discrete", values=[24]),
    ],
    objectives=[Objective(metric="energy_cost", direction="minimize")],
    constraints=[Constraint(metric="departure_charge_pct", minimum=80)],
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


async def main():
    service = ExperimentService(SyntheticWattwiseAdapter())
    plan = service.design(spec)
    print(plan.design_info["type"], len(plan.runs), "runs; randomisation:", plan.randomization["scheme"])
    result = await service.execute(plan, spec)
    analysis = service.analyse(result)

    best = next(r for r in result.runs if r.run_id == analysis.best_run_id)
    print("cheapest feasible:", best.configuration)
    worst_cheap = min(analysis.infeasible_runs, key=lambda r: r["objectives"]["energy_cost"])
    print("cheaper but rejected:", worst_cheap["configuration"], "->", worst_cheap["violations"][0]["value"], "% at departure")

    for e in analysis.main_effects:  # per-factor-level comparisons, never pooled
        if e.factor == "charge_start_hour" and e.evidence == "sufficient":
            print(f"start {e.control_level}->{e.level}: {e.mean_difference:+.2f} cost, 95% CI {e.confidence_interval_95}")
    reg = analysis.regression[0]
    print("R^2", round(reg.r_squared, 3), "| warnings:", reg.warnings[:1])

    report = service.report(result, analysis)
    print(ReportEngine.to_json(report)[:60].replace("\n", " "), "...")
    print(ReportEngine.to_csv(result).splitlines()[0])


asyncio.run(main())
```

## 3. Reading the results

* `analysis.effects`: configuration-vs-control comparisons; `main_effects`: factor level vs reference. Check `evidence`: with a single
  replicate per configuration, intervals are **withheld** (`"insufficient"`). That is why the design above uses `replicates: 2`.
* `analysis.anova` / `regression` / `interactions`: adjusted inference; read their `warnings`. In this tutorial the regression reports a
  low R² and a Breusch-Pagan warning: charge start hour enters the regression as a *linear numeric* factor, but cost is a non-linear,
  time-of-day-dependent function of it. The engine tells you the linear model is inadequate; rely on the direct level comparisons
  (and the categorical-level ANOVA), or declare the factor `categorical` to model each start hour separately.
* `analysis.best_run_id`, `pareto_run_ids`, `infeasible_runs`: feasibility-aware; see [statistics](../architecture/statistics.md).
* `report.limitations` always includes the analysis limitations and, for synthetic adapters, a SYNTHETIC-data notice.

## 4. Persist, restart, continue

```python
from trialforge import SQLiteExperimentRepository

service = ExperimentService(adapter, repository=SQLiteExperimentRepository("sqlite:///wattwise.db"))
# ... later, in a new process:
service = ExperimentService(adapter, repository=SQLiteExperimentRepository("sqlite:///wattwise.db"))
await service.recover()  # settle anything interrupted
result = service.get(experiment_id)  # everything is still there
```

## 5. Bring your own data

`examples/wattwise_meter_adapter/adapter.py::measurements_from_csv` converts a `timestamp,power` meter export (kW or W) into measurements
tagged `reading_kind="imported"`; `await service.ingest_measurements(experiment_id, run_id, measurements)` attaches them to a planned run
(data collected outside TRIALFORGE). Completed runs are immutable; re-delivering the same measurement id is ignored.
