# TRIALFORGE

A domain-independent Python SDK for **designing, running, measuring, analysing and optimising experiments**.
Applications plug their own systems in through *adapters*; the core knows nothing about any particular domain.

```text
ExperimentSpec ─► ExperimentDesigner ─► ExperimentService ─► MeasurementEngine ─► Analyzer ─► Optimizer ─► ReportEngine
 (factors,         (factorial, OFAT,     (lifecycle,         (units, quality,     (effects,    (GP/EI,       (JSON / CSV,
  objectives,       fractional, blocked,   adapters,           time series,         ANOVA,       Optuna,       versioned,
  constraints)      CCD, Box-Behnken)      retries, recovery)  provenance)          regression)  stopping)     reproducible)
                                                  └──────── SQLite / SQLAlchemy persistence (restart-safe) ────────┘
```

Three example adapters live in [`examples/`](examples/) and are built *only* on the public SDK:

| Example | Domain | Data |
|---|---|---|
| [`f1_simulator_adapter`](examples/f1_simulator_adapter/adapter.py) | F1 SETUP LAB, vehicle setup | **synthetic** vehicle model + telemetry-CSV import |
| [`wattwise_meter_adapter`](examples/wattwise_meter_adapter/adapter.py) | WATTWISE LAB, household electricity | **simulated** smart meter + CSV import, tariff costing |
| [`third_domain_adapter`](examples/third_domain_adapter/adapter.py) | coffee roasting (unrelated domain) | **synthetic** deterministic roaster |

> None of the example data is real. There is no connection to any Formula 1 team, live telemetry or physical meter.
> This repository contains the **core SDK and adapters only**; the two complete frontend applications are future work.

## Install

```bash
pip install -e ./packages/trialforge-core            # core (Python 3.11+)
pip install -e "./packages/trialforge-core[optuna]"  # + Optuna optimisation strategy
pip install -e "./packages/trialforge-core[dev]"     # + tests, reference implementations, ruff
```

## Thirty-second tour

```python
# docs-test
import asyncio
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
from examples.third_domain_adapter.adapter import CoffeeRoastAdapter

spec = ExperimentSpec(
    experiment_id="readme-roast",
    name="Roast study",
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
    run_budget=40,
    random_seed=7,
)


async def main():
    registry = AdapterRegistry()
    registry.register(CoffeeRoastAdapter())
    service = ExperimentService(registry=registry)
    result = await service.run(spec)  # design + execute, persisted automatically
    analysis = Analyzer().analyse(result)
    print(len(result.runs), "runs; best run:", analysis.best_run_id)
    print(Optimizer().suggest(spec, result, count=1)[0].rationale)


asyncio.run(main())
```

Run it from the repository root (so that `examples` is importable): `python -m examples.quickstart` runs a longer version.

## Documentation

| | |
|---|---|
| Getting started | [Installation](docs/getting_started/installation.md) · [Quickstart](docs/getting_started/quickstart.md) |
| Concepts | [Architecture](docs/architecture/overview.md) · [Statistical methods](docs/architecture/statistics.md) · [Execution & recovery](docs/architecture/execution_and_recovery.md) |
| Building apps | [Adapter development guide](docs/adapter_development/guide.md) · [Tutorial: from design to report](docs/tutorials/first_experiment.md) · [Tutorial: optimisation loop](docs/tutorials/optimisation_loop.md) |
| Reference | [API reference](docs/api_reference/index.md) (generated) · [Limitations](docs/limitations.md) |
| Project | [Kiro spec, steering and hooks](.kiro/) |

## Capabilities at a glance

- **Designs**: full factorial, OFAT, random, fractional factorial (defining relations, alias structure, resolution), blocked
  (randomised complete blocks and confounded blocks), central composite, Box-Behnken, sequential extension. Seeded and reproducible.
- **Execution**: lifecycle state machine, multiple adapters selected per experiment, retries, idempotency, cancellation,
  delayed measurements, concurrency limits, timeouts, restart recovery driven by adapter capabilities.
- **Measurements**: validation, unit conversion, quality taxonomy (invalid / missing / outlier / sensor warning), time series
  (windows, resampling, streaming), derived metrics, transformations, full provenance.
- **Statistics**: per-treatment and per-factor-level comparisons (no pooling), Welch / paired / bootstrap intervals, one-way and
  Type II factorial ANOVA, OLS with diagnostics, interactions, constraint-aware Pareto analysis.
- **Optimisation**: Gaussian-process Bayesian optimisation with Expected Improvement, probability-of-feasibility constraints,
  ParEGO multi-objective, Optuna, grid / random, labelled heuristic, stopping rules.
- **Persistence**: SQLAlchemy (SQLite default), transactional run commits, durable job queue, migrations from 0.1 databases.
- **Reports**: versioned JSON, CSV exports, reproducibility metadata.
- **Extensibility**: explicit plug-in registry for metrics, transformations, objectives, constraints, optimisers and report sections.

See [Limitations](docs/limitations.md) before relying on the SDK for anything important.
