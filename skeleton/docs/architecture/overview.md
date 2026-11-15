# Architecture

TRIALFORGE core is a layered library. Dependencies point **downward only**; `tests/test_architecture.py` enforces this.

```text
                 ┌───────────────────────── execution ─────────────────────────┐
                 │  ExperimentService · LocalBackgroundRunner                  │   orchestrates everything below
                 └─┬────────┬───────────┬──────────┬─────────┬────────┬───────┘
          designer │ adapters│ analysis  │ optimization │ reporting │ persistence · history
                 └──┴────────┴─────┬─────┴──────────┴─────────┴────────┘
                                   │  evaluation   (outcomes, feasibility, objective orientation, Pareto)
                                   │  measurements (validation, units, time series, provenance)
                                   │  models       (validated pydantic types, lifecycle tables)
                          errors · utilities (safe expressions) · definitions (plug-in registry) · events
```

Layer rules (see `LAYERS` in the architecture test): `models` knows only `errors` and `utilities`; `analysis`, `optimization` and
`reporting` never import `execution` or `persistence` (reporting reads only `spec_hash` from persistence); `adapters` knows only models.
The core **never imports an example or any application module** (also tested), and the examples import **only** `from trialforge import ...`.

## Package map (`packages/trialforge-core/src/trialforge/`)

| Package | Responsibility |
|---|---|
| `models/` | `ExperimentSpec`, `Factor`, `Objective`, `Constraint`, `MeasurementDefinition`, `DerivedMetric`, `Run`, `DesignPlan`, results, lifecycle transition tables |
| `designer/` | `ExperimentDesigner` and `classical.py` (pure matrix generation, aliasing, resolution) |
| `adapters/` | adapter protocol, metadata/capabilities, `AdapterRegistry` (selection, capability checks, lifecycle hooks) |
| `execution/` | `ExperimentService` (lifecycle, retries, recovery, sequential loop), `LocalBackgroundRunner` |
| `measurements/` | `MeasurementEngine`, `UnitRegistry`, quality taxonomy, time-series helpers |
| `evaluation/` | shared definition of *outcome*, *feasible*, *oriented objective*, *Pareto* used by analysis, optimiser and reports |
| `analysis/` | `AnalysisEngine`, `stats.py` (numerics) |
| `optimization/` | `Optimizer`, Gaussian-process `surrogate.py`, stopping rules |
| `persistence/` | SQLAlchemy repository, schema, migrations, job queue |
| `history/` `reporting/` | read-only history views; report assembly and exports |
| `definitions/` | `PluginRegistry` (explicit extension points) |
| `events/` `errors/` `utilities/` | event bus, exception hierarchy, restricted expression evaluator |

## Lifecycle

```text
Experiment: CREATED → VALIDATED → DESIGNED → QUEUED → RUNNING ⇄ WAITING_FOR_MEASUREMENTS → COMPLETED | FAILED | CANCELLED
Run:        CREATED → QUEUED → RUNNING → WAITING_FOR_MEASUREMENTS → COMPLETED | FAILED | CANCELLED
            FAILED → QUEUED only through an explicit retry_run();  COMPLETED and CANCELLED are terminal
```

Transitions are validated against `RUN_TRANSITIONS` / `EXPERIMENT_TRANSITIONS`; the experiment status is *derived* from its runs and moved
only through legal intermediate states. Every transition is persisted to the `state_log` table.

## Data flow of one run

1. `ExperimentService` pre-validates every queued configuration (domains, input constraints) **before any adapter call**.
2. The run is persisted as `RUNNING` (attempt counter incremented) **before** the adapter is invoked, so a crash is visible.
3. The adapter returns measurements (immediately, or a handle for later `collect_result`).
4. `MeasurementEngine.assess` copies the measurements, binds placeholder run ids, converts units (keeping `raw_value`/`raw_unit`),
   flags invalid / duplicate / out-of-range / outlier / sensor-warning data, and reports missing required metrics. Nothing is deleted.
5. `process` aggregates usable samples per metric, applies registered transformations, computes derived metrics, and records a
   `MeasurementTransformation` history plus source measurement ids.
6. The run, raw and processed measurements and the experiment status are committed in **one database transaction**. If the commit fails
   the run does not become COMPLETED.

## Persistence schema (SQLite by default)

`experiments`, `plans`, `runs`, `measurements`, `processed_measurements`, `analyses`, `recommendations`, `reports`, `adapters`, `jobs`,
`state_log`, `schema_info`. `SQLiteExperimentRepository.migrate()` creates the schema and imports databases written by 0.1.0 (the legacy
snapshot table is kept as `experiments_legacy_v1`). Python callables (`Factor.validation_rule`, `Constraint.validator`) cannot be
serialised: a reloaded spec carries `metadata["_callables_not_restored"]` and `ExperimentService.attach_spec()` re-supplies the original.
The spec of an experiment that already has runs cannot be changed (reproducibility).

## Extension points

`PluginRegistry` (explicit registration, no dynamic imports, no `eval`): `metric`, `transformation`, `objective`, `constraint`,
`optimizer`, `report`. Specs refer to plug-ins by *name*; unknown names are rejected at validation. See the
[adapter development guide](../adapter_development/guide.md).
