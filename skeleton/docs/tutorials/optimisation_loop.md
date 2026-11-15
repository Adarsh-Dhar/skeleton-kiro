# Tutorial: the sequential optimisation loop

```python
# docs-test
import asyncio
from trialforge import ExperimentService, ExperimentSpec, Factor, MeasurementDefinition, Objective, StoppingCriteria, Constraint
from examples.f1_simulator_adapter.adapter import SyntheticF1Adapter

spec = ExperimentSpec(
    experiment_id="tutorial-f1-opt",
    name="Setup refinement (synthetic data)",
    factors=[
        Factor(name="front_wing", factor_type="continuous", lower=10, upper=40),
        Factor(name="rear_wing", factor_type="continuous", lower=10, upper=40),
        Factor(name="suspension", factor_type="continuous", lower=3, upper=9),
    ],
    objectives=[Objective(metric="lap_time", direction="minimize")],
    constraints=[Constraint(metric="stability", minimum=85)],
    measurement_definitions=[
        MeasurementDefinition(metric="lap_time", unit="seconds"),
        MeasurementDefinition(metric="stability", unit="score"),
    ],
    design_method="random",
    design_options={"n_runs": 8},
    run_budget=24,
    random_seed=5,
)


async def main():
    service = ExperimentService(SyntheticF1Adapter())
    await service.run(spec)  # initial space-filling design
    result, stop = await service.run_sequential(
        spec.experiment_id,
        iterations=6,
        batch_size=2,
        method="bayesian",
        criteria=StoppingCriteria(min_improvement=0.002, patience=4),
    )
    print(len(result.runs), "runs; stopped because:", stop.reason, "-", stop.detail)
    for rec in service.history(spec.experiment_id).recommendations()[:2]:
        print(rec.strategy, rec.prediction_kind, "EI =", rec.expected_improvement and round(rec.expected_improvement, 4))


asyncio.run(main())
```

## What each strategy returns (and does not claim)

| `method` | What it is | `prediction_kind` | `expected_improvement` |
|---|---|---|---|
| `bayesian` (alias `sequential`) | Matérn-5/2 Gaussian process, analytic Expected Improvement, probability-of-feasibility for modelled output constraints, ParEGO scalarisation for several objectives | `model_prediction` | set (units stated in `uncertainty`) |
| `optuna` | Optuna sampler seeded with your history (GP sampler if PyTorch is installed, else TPE; multi-objective uses TPE) | `search` | `None` |
| `grid`, `random` | unseen feasible points | `search` | `None` |
| `heuristic` | the 0.1 inverse-distance score, **uncalibrated** | `heuristic_score` | `None` |

With too few observations (fewer than `max(4, encoded_dims + 1)`) `bayesian` falls back to random search and *says so* in the
rationale. Recommendations never repeat an executed configuration, never violate input constraints, and carry `remaining_budget`,
`constraint_evaluation` (input: evaluated; output: predicted probability or "unknown until measured") and a rationale.

Output constraints that cannot be modelled (equality, allowed values, custom functions) are listed as *not modelled* and are checked only
after measurement. The best run reported by analysis only ever comes from runs that actually satisfied every constraint.

## Stopping rules

`Optimizer.should_stop(spec, result, StoppingCriteria(...))` (also used by `run_sequential`) checks, in order: `user_cancelled`
(`criteria.cancelled()`), `target_achieved` (best *feasible* value reached `target_value`), `budget_exhausted`,
`improvement_threshold_met`, `max_iterations`, `insufficient_improvement` (less than `min_improvement` over the last `patience` runs)
and `no_feasible_candidate`. Target/threshold/stagnation rules apply to single-objective experiments.

## Sequential design

`ExperimentDesigner.sequential(spec, result, configurations)` / `ExperimentService.add_runs(...)` append validated runs (domains, input
constraints, remaining budget) as `QUEUED`; the stored plan records them in `design_info["sequential_extensions"]`.
