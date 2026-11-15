# TRIALFORGE completion: design

## 1. Initial repository assessment (audit of 0.1.0; source code was the source of truth)

The previous report was **not** fully accurate. Verified by reading all 3 700 lines of source and running the tests (8 pass; 2 test
modules failed collection because `examples/` did not exist; README linked to non-existent paths and `trialforge.examples`).

| Area | Found in 0.1.0 | Verdict |
|---|---|---|
| Models | Reasonable pydantic models; one `Constraint` type for factors *and* metrics; no block factors, derived metrics, policies; no per-estimate provenance | partial |
| Designer | factorial/OFAT/random only; factorial silently dropped to 2 levels over budget; no fractional/blocked/RSM | partial |
| Execution | one adapter; `AdapterRegistry` unused; `_specs` dict never populated; in-memory state; no retries/recovery/concurrency; background = none | incorrect / missing |
| Measurements | validation + exact-string unit match; no conversion, windows, derived metrics, streaming, outlier flags; any flag excluded a measurement | partial |
| Analysis | all non-control runs pooled as one "treatment"; mixed metrics in `factor_effects`; normal-approximation (1.96) CI; no ANOVA/regression/interactions; Pareto ignored constraints | **incorrect** |
| Optimisation | inverse-distance weighting presented as `expected_improvement`; output constraints not enforced; no stopping rules | **misleading** |
| Persistence | snapshot repository, not used by the service | missing integration |
| Reporting | small JSON/CSV report | partial |
| Examples/docs | none present; dead links | missing |
| Tests | 8 passing, 2 uncollectable | inadequate |

Everything that worked (model shapes, lifecycle names, `Recommendation` fields, public service method names, run-id derivation) was kept;
changes are additive or documented in the report as behaviour changes (feet→metres now converts; `sequential` now means Bayesian; the
factorial no longer degrades silently).

## 2. Architecture decisions

* **Single source of truth for "outcome / feasible / best"**: `trialforge.evaluation` (new layer), used by analysis, optimiser, reports and
  history, replacing three diverging copies.
* **Repository is authoritative**: the service persists each transition and reloads from the repository; the only in-memory structure is
  an *override* map for specs with callables (`attach_spec`).
* **Capabilities drive behaviour** (concurrency, retries, recovery) instead of assumptions.
* **Honest numerics**: statistics withhold inference when unjustified; optimiser labels what each number means.
* **Own implementations over heavy dependencies** where correctness could be verified independently: classical designs (generators,
  defining relation, resolution computed, not looked up), GP + EI (checked against Monte Carlo), OLS/ANOVA (checked against statsmodels).
* **Layering enforced by test** (`tests/test_architecture.py`).
* **Single contract owner**: `models/core.py` and `adapters/contracts.py` are the only definitions of shared types (see `.kiro/agents`).

## 3. Cross-module contract changes (all backward compatible)

`Factor.role`; `Constraint.kind/function`; `Objective.function/priority`; `MeasurementDefinition.transformations/outlier_method`;
`ExperimentSpec.adapter_name/design_options/execution/derived_metrics`; `Run.block/coded/attempts/idempotency_key/history`;
`DesignPlan.design_info/randomization/block_variables`; `Recommendation.prediction_kind/predicted_value/...`;
`Measurement.raw_value/raw_unit/source/channel`; `AdapterCapabilities` (+concurrent, resumable, idempotent...);
`ExecutionContext.metadata["requested_metrics"]`. `trialforge.errors` hierarchy; `ValidationError`/`DesignError` subclass `ValueError`.

## 4. Verification strategy

Independent references (scipy, statsmodels, numpy lstsq, closed forms, Monte Carlo, coverage simulation), mutation checks of
concurrency tests, crash simulation by task cancellation against a file database, one contract suite over all adapters,
architecture/AST tests, executed documentation. See `tasks.md` for evidence per task.
