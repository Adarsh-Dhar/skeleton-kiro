# TRIALFORGE completion: tasks (dependency order) and evidence

Checkpoints (**CP**) are the exit conditions from the master prompt. Evidence names the implementing module and the verifying tests
(`P` = `packages/trialforge-core/tests`, `T` = `tests`).

## Priority 0: correctness of existing defects
- [x] 0.1 Audit all source and tests (see `design.md` §1)
- [x] 0.2 Fix treatment/control pooling → `analysis/engine.py` (`_configuration_comparisons`, `_factor_level_comparisons`); P `unit/test_statistics.py`, P `regression/test_audit_defects.py`
- [x] 0.3 Per-factor analysis, interactions → `analysis/engine.py` (`_interactions`, `_anova`, `_regression`); P `unit/test_statistics.py`
- [x] 0.4 t-based / Welch / paired / bootstrap intervals → `analysis/stats.py`; coverage simulation in `test_statistics.py`
- [x] 0.5 Output-constraint enforcement → `evaluation/__init__.py`; P `test_statistics.py` (feasibility, unknown outcomes)
- [x] 0.6 Spec retrieval without hidden cache → `execution/service.py::get_spec`; P `integration/test_persistence_recovery.py`
- [x] 0.7 Registry integration → `adapters/contracts.py::AdapterRegistry.select`; P `integration/test_execution_lifecycle.py`
- [x] **CP0** statistical + execution correctness tests pass

## Priority 1: statistics, designs, measurements, optimisation
- [x] 1.1 Designs (factorial, OFAT, random, fractional, blocked, CCD, BB, sequential) → `designer/`; P `unit/test_designs.py`
- [x] 1.2 Measurement engine (units, quality, series, derived, streaming) → `measurements/`; P `unit/test_measurements_engine.py`
- [x] 1.3 ANOVA/regression/diagnostics → `analysis/`; cross-checked with statsmodels/scipy/numpy
- [x] 1.4 Optimiser (GP/EI, constraints, ParEGO, Optuna, heuristic label, stopping) → `optimization/`; P `unit/test_optimization.py`
- [x] **CP1** algorithms implemented and independently verified

## Priority 2: execution and persistence
- [x] 2.1 Repository + automatic persistence + transactions + migration → `persistence/repository.py`
- [x] 2.2 Lifecycle, retries, idempotency, cancellation, delayed, concurrency, timeouts → `execution/service.py`
- [x] 2.3 Restart recovery by capability → `ExperimentService.recover`; P `integration/test_persistence_recovery.py`
- [x] 2.4 History and reporting → `history/`, `reporting/`; P `integration/test_workflows.py`
- [x] **CP2** state survives restart and is reproducible

## Priority 3: extension system
- [x] 3.1 Adapter contracts + registry, 3.2 plug-in registry (metrics, transformations, objectives, constraints, optimisers, reports)
- [x] **CP3** third-party adapter without core edits; T `test_architecture.py::test_brand_new_domain_adapter...`

## Priority 4: delivery
- [x] 4.1-4.3 Three adapters (`examples/`), contract suite P `contracts/`
- [x] 4.4 Architecture tests T `test_architecture.py`; 4.5 package install verified in a clean virtualenv (see report)
- [x] 4.6 Documentation + runnable examples; T `test_docs.py`
- [x] 4.7 Completion report
- [ ] Out of scope / future: F1 SETUP LAB and WATTWISE LAB frontends; non-SQLite database verification; distributed execution
