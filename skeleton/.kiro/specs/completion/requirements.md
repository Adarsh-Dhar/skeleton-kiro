# TRIALFORGE completion: requirements

Source: the *Master Continuation & Completion Prompt*. This is a **continuation** of the 0.1.0 implementation: preserve working code,
fix defects, implement what is missing, verify everything by executing it. Scope: TRIALFORGE CORE and three example adapters (no
frontends).

Each requirement carries an ID used by `tasks.md` and by the completion report. Status values: IMPLEMENTED AND VERIFIED,
IMPLEMENTED BUT NOT FULLY VERIFIED, PARTIALLY IMPLEMENTED, NOT IMPLEMENTED, BLOCKED BY EXTERNAL DEPENDENCY.

| ID | Requirement (abridged) |
|---|---|
| R1 | Independently installable, versioned SDK with stable public imports; core never imports application code |
| R2 | Typed models: continuous/discrete/categorical factors; minimise/maximise/target; several objectives; input **and** output constraints; comprehensive validation; deprecation-safe |
| R3 | Designs: OFAT, full factorial, randomised order, fractional (relations, aliases, resolution), blocked, reproducible, budget-enforced, sequential; CCD/Box-Behnken only if correct |
| R4 | Execution: multiple adapters selected per experiment, lifecycle states + enforced transitions, retries, idempotency, cancellation, delayed measurements, concurrency control, timeouts, no hidden spec cache |
| R5 | Restart recovery driven by adapter capabilities; interrupted runs never silently completed or duplicated; durable queue |
| R6 | Adapter framework: protocol, metadata, capabilities, schemas, registry; explicit plug-ins for metrics, transformations, objectives, constraints, optimisers, reports; no dynamic code execution |
| R7 | Measurements: validation, unit conversion, duplicates/missing/invalid, quality flags (never auto-delete outliers), aggregation, derived metrics, transformations, provenance, time series (windows, resampling, streaming) |
| R8 | Statistics: no pooling of treatments; per-factor/level and configuration comparisons; descriptive stats; effects; t-based/Welch/paired/bootstrap intervals; insufficient-evidence handling; ANOVA; regression + diagnostics; interactions; constraint-aware multi-objective; full analysis metadata |
| R9 | Optimisation: grid, random, sequential, Bayesian (GP/EI), Optuna, constraint-aware, multi-objective, honest labelling of heuristics, stopping rules |
| R10 | Persistence: SQLAlchemy, automatic persistence of every transition, transactional commits, migrations, restart tests |
| R11 | Reporting: versioned JSON/CSV with the 22 required sections; no plotting dependencies |
| R12 | Three adapters (F1 synthetic, WATTWISE simulated, coffee) on the public SDK only; synthetic data clearly labelled |
| R13 | Developer experience: install guide, architecture docs, API docs, adapter tutorial, runnable examples, every public class/method documented |
| R14 | Verification: unit, integration, contract (same suite for all adapters), statistical (independent references), reproducibility, architecture tests; actual pass/fail counts reported |
| R15 | Kiro workflow artifacts: spec, steering, hooks, agents committed |
