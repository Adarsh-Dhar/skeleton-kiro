# Statistical methods and their limits

Correctness is prioritised over breadth. Everything below is implemented in `analysis/engine.py` and `analysis/stats.py` and verified
in `packages/trialforge-core/tests/unit/test_statistics.py` against scipy, statsmodels, closed-form formulas and constructed datasets.

## Unit of analysis

The **run**. A run's raw samples are first aggregated (per the metric's `aggregation`) into one outcome per metric, so repeated samples
inside a run are never treated as independent replicates. Only usable (not invalid) measurements are used; outliers are *flagged but
kept* unless the analysis is configured with `exclude_outliers=True`.

## Comparisons (never pooled)

* **Configuration vs control**: each distinct non-control configuration is compared with the control configuration. If exactly one
  factor differs it is reported as a single-factor effect (`factor`, `level`, `control_level`); otherwise as a `configuration` comparison
  listing `differing_factors`.
* **Factor level vs reference level** (`main_effects`): marginal comparison across runs. Flags imbalance and states that it ignores other
  factors; the ANOVA/regression results are the adjusted ones.
* **Paired** (`ci_method="paired t"`): used automatically when both sides have runs in the same ≥2 blocks.
* Every estimate lists the control and treatment run ids it used. Holm-adjusted p-values are provided per comparison family.

## Inference

* Welch's t (Satterthwaite df) intervals and tests; paired t; Hedges' g / Cohen's dz effect sizes; optional percentile bootstrap
  (`bootstrap=True`, ≥3 per group). The `confidence_interval_95` field is a t-based interval; a normal approximation is never used.
* **Insufficient evidence is explicit**: with fewer than 2 observations in either group no standard error, interval or p-value is produced
  (`evidence="insufficient"`, with a warning). Zero-variance groups also yield no interval.
* Shapiro-Wilk is checked per group and on residuals; small samples (<5 per group) produce warnings.

## ANOVA, regression, interactions

* **One-way ANOVA** per factor (matches `scipy.stats.f_oneway`). **Factorial Type II ANOVA** (matches statsmodels) with main effects, all
  two-way treatment interactions, and block factors as additive terms. If the interaction model leaves no residual df the engine falls
  back to main effects only *and says the F tests then assume no interaction*; if even that is saturated the table is withheld.
* **OLS regression**: numeric factors coded to [-1, +1], categoricals dummy-coded; quadratic terms for central-composite/Box-Behnken
  designs. Aliased columns (e.g. in fractional designs) are dropped, main effects first, with a warning. Diagnostics: R², adjusted R²,
  F test, Shapiro (residuals), Durbin-Watson (run order), Breusch-Pagan, Cook's distance, leverage, studentised-residual outlier runs,
  condition number.
* **Interaction effect** = difference of differences of cell means at the extreme levels (all four cells required), with the ANOVA
  F test when estimable.

## Multi-objective analysis

Run-level objective vectors are oriented so smaller is better (`maximize` negated, `target` = absolute deviation). **Infeasible runs (any
violated or unknown input/output constraint) are excluded from the Pareto front and from `best_run_id`**; they are listed with their
violations and whether they would have dominated the best feasible run. `best_run_id` minimises a weighted sum of min-max-normalised
objectives among feasible runs (scale-free weights); use the Pareto front for trade-offs. No feasible run ⇒ no best run.

## What the analysis does *not* claim

Associations from non-randomised or confounded data are not causal; fractional designs alias effects (reported); p-values are unadjusted
unless `p_value_holm` is used; assumptions are checked but not enforced; no mixed-effects or non-linear models; no power analysis.
