# Changelog
## 0.2.2
- **Fix (statistics)**: the factorial Type II ANOVA no longer dummy-codes every distinct value of a continuous factor. Found in
  F1 SETUP LAB: a 16-run random design with four continuous factors (16 distinct settings each) produced a saturated model with
  no residual degrees of freedom, so the ANOVA was withheld although the regression had 5. Encoding now follows the factor type:
  continuous factors are coded to [-1, +1] (as in the regression), categorical factors use dummy columns, discrete factors use one
  dummy column per declared level in the ANOVA (identical to the coded column for two levels) and a coded linear column in the
  regression. Central composite and Box-Behnken designs now include the squared terms in the ANOVA as well as the regression, and
  Type II adjustment excludes every term that contains the tested term (a main effect is tested without its squared term).
- **Behaviour change**: marginal factor-level comparisons (`AnalysisResult.main_effects`) are no longer produced for continuous
  factors, whose settings are numbers, not categories; use the regression/ANOVA. Discrete and categorical factors are unchanged.
  The factorial ANOVA of a design with centre points no longer detects curvature through three-level dummy coding; use a
  response-surface design for curvature.
- **Added (optional field)**: `AnovaTable.model_info` (parameters, rank, residual df, distinct settings per factor, replicated
  configurations, pure-error df, whether squared terms are included) and `RegressionModel.diagnostics["model_info"]`. New warnings
  state a saturated model exactly, low residual df (< 10) and that a model has no squared terms. `ALGORITHM_VERSION` is 2.1.

## 0.2.1
- **Fix**: `ExperimentService.design()` no longer persists the specification before the design succeeds. A rejected design
  (budget too small, incompatible method, no feasible runs) previously left a plan-less experiment in the repository.
  Found by building F1 SETUP LAB on the SDK; regression test added.
- No API changes. Applications pinned to `trialforge-core>=0.2.0,<0.3` are unaffected.

## 0.2.0
- First complete SDK release: designs, execution/recovery, measurements, statistics, optimisation, persistence, reporting, plug-ins.
