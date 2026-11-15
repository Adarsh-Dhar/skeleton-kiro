# Limitations and status

This is a working, tested SDK, **not** a certified or production-hardened system. Known limits:

**Scope.** Core SDK and three example adapters only. The F1 SETUP LAB and WATTWISE LAB applications are not built. All example data is
synthetic or simulated.

**Statistics.**
* Regression treats numeric (continuous and discrete) factors as linear terms; declare a factor `categorical` to model levels freely.
  The one-way/factorial ANOVA already treat levels as categories.
* Linear models only (no mixed effects, GLMs, non-linear least squares); no power or sample-size planning.
* Factorial ANOVA includes two-way interactions at most; higher-order interactions are not modelled.
* Marginal factor-level comparisons assume balance (flagged when unbalanced); p-values are unadjusted except `p_value_holm`.
* Causal interpretation requires proper randomisation and control of nuisance variables, which the SDK cannot verify.
* Fractional designs alias effects; the report lists the aliasing, and regression drops dependent terms (main effects first).
* Box-Behnken is limited to 3-5 factors; central composite uses a full factorial core up to 5 factors (or supply generators for a
  resolution V fraction). CCD axial points sit on the factor bounds (inscribed design) because bounds are hard limits.

**Optimisation.** The built-in Gaussian process has no categorical kernel (one-hot inputs), is fitted on all history (O(n³)), and its
uncertainty is only as good as its smoothness/stationarity assumptions. Output constraints with equality, allowed-value or function form
are not modelled. Optuna's GP sampler needs PyTorch. Multi-objective Optuna uses TPE.

**Execution.** Single-process asyncio engine; there is no distributed queue. `LocalBackgroundRunner` lives in the process' event loop.
Recovery quality depends on adapters declaring truthful capabilities. A run whose adapter raised `asyncio.CancelledError` mid-flight is
left `RUNNING` by design (it simulates a crash) until `recover()` runs.

**Persistence.** Tested with SQLite only. Callables in specs are not persisted (see architecture). Concurrent *processes* writing the same
SQLite file are not tested.

**Measurements.** Unit registry covers common SI/imperial units; unknown units convert only to an identical string. Outlier flagging needs
≥5 samples per (metric, channel) within a run. Resampling is linear interpolation without extrapolation.

**Kiro workflow.** `.kiro/` documents the spec, steering, hooks and agents used for this work. Hooks call the scripts in `scripts/`
(verified); the hook JSON itself could not be exercised outside Kiro.
