# Changelog

## 0.2.1
- **Fix**: `ExperimentService.design()` no longer persists the specification before the design succeeds. A rejected design
  (budget too small, incompatible method, no feasible runs) previously left a plan-less experiment in the repository.
  Found by building F1 SETUP LAB on the SDK; regression test added.
- No API changes. Applications pinned to `trialforge-core>=0.2.0,<0.3` are unaffected.

## 0.2.0
- First complete SDK release: designs, execution/recovery, measurements, statistics, optimisation, persistence, reporting, plug-ins.
