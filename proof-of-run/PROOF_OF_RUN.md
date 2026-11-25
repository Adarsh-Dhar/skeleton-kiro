# Proof of run

What was run, on what, and what came out. The raw logs are in this folder; `SHA256SUMS.txt` lists their SHA-256 hashes, so a later
edit to a log is detectable. Nothing here was edited by hand except this summary.

## Run identity
| | |
|---|---|
| Command | `TRIALFORGE_PID_DIR=<tmp> F1_PORT=18001 WATTWISE_PORT=18002 INSTALL_CHROMIUM=1 RUN_NETWORK_TESTS=1 RUN_START_SMOKE=1 scripts/check-release.sh` |
| Started / finished | 2026-11-25T20:13:42Z / about 20:26:40Z (about 13 minutes) |
| Machine | macOS (Darwin 27.0.0, arm64) |
| Python / pandas / numpy / scipy | 3.13.11 / 3.1.1 / 2.5.5 / 1.18.1 |
| SDK installed from the built wheel | trialforge-core 0.2.2 (site-packages, not the source tree) |
| Playwright / Chromium | 1.65.2, installed by the run |
| Git base commit | `b031f2a`, with uncommitted working-tree changes (the code under test is the working tree, not a commit) |
| Environment | a fresh release virtual environment created by the script |
The smoke check used ports 18001/18002 and a throw-away pid directory and database, so the F1 app running on port 8001 and `data/trialforge.db` were not touched.

## Result: 34 passed, 0 failed, 3 skipped, 0 unavailable
Full table: `release-check.log`. Per-suite output: `suite-logs/`.
| Check | Result | Log |
|---|---|---|
| Build SDK wheel 0.2.2, install it, build both app wheels | PASSED | `release-check.log` |
| Both apps import and use the installed SDK 0.2.2; F1 has no FastF1 | PASSED | `release-check.log` |
| Lint: root, F1, WATTWISE, SDK | PASSED (4) | `suite-logs/lint__*.log` |
| Python compile, JavaScript syntax, shell syntax | PASSED (3) | `release-check.log` |
| SDK tests | 263 passed | `suite-logs/SDK_tests__trialforge_core__.log` |
| SDK workspace tests | 54 passed | `suite-logs/SDK_workspace_tests__skeleton_tests__.log` |
| Root tests (database, migrations, scoping, start scripts) | 120 passed, 1 skipped | `suite-logs/root_tests__*.log` |
| F1 SETUP LAB backend tests | 283 passed, 5 skipped | `suite-logs/F1_SETUP_LAB_backend_tests_.log` |
| WATTWISE LAB backend tests (includes the property tests) | 252 passed, 1 xfailed | `suite-logs/WATTWISE_LAB_backend_tests_.log` |
| F1 browser tests (Chromium) | 7 passed | `suite-logs/F1_SETUP_LAB_browser_tests__Chromium__.log` |
| WATTWISE browser tests (Chromium) | 9 passed | `suite-logs/WATTWISE_LAB_browser_tests__Chromium__.log` |
| Real OpenF1 imports, five sessions 2023-2026, live network | PASSED (5 tests, 5 min 14 s) | `suite-logs/openf1-live.log` |
| Startup smoke: both apps healthy together, shared database migrated, one stops while the other keeps running, ports released | PASSED (6 checks) | `suite-logs/start-f1.log`, `start-wattwise.log` |

### Skipped, and why (skips are not passes)
- Root `tests/test_start_scripts.py:110`, F1 `tests/test_openf1.py:394`: the opt-in tests are not run inside pytest. The script runs them as their own checks above, and both passed.
- Real household meter export, tariff and bill reconciliation: a manual gate that needs your data. It has not been done.

## Evidence for the specific changes in this work
| Change | Evidence |
|---|---|
| Migration 6 creates the version-6 schema; upgrades from 6, 9, 10 | `migration-tests-verbose.log`: 15 passed |
| Continuous-factor ANOVA, quadratic terms, level comparisons, 16-run and CCD cases | `provenance-and-analysis-tests-verbose.log` (continuous-analysis tests) and `suite-logs/SDK_tests__trialforge_core__.log` |
| Source label, counts, notices, CSV `source` column, historical not reachable as experiments | `provenance-and-analysis-tests-verbose.log`: 16 passed (includes `test_provenance.py` and the Chromium `test_provenance_ui.py`) |
| Gaps, standings reason, tyre-trend wording, weather dropout mask | `provenance-and-analysis-tests-verbose.log` (report tests) |

## Found and fixed after the main run
The start-script log `suite-logs/start-f1.log` shows `scripts/start-f1.sh: line 11: name: unbound variable`. Cause: the EXIT trap in `scripts/lib/common.sh`
read a function-local variable that no longer existed, so the pid file was not removed on exit. Fixed, with a regression test. Evidence run after the fix:
- `start-stop-after-trap-fix.log`: start on a throw-away port, health OK, stop; pid file removed, port released, 0 "unbound variable" messages.
- `root-tests-after-trap-fix-verbose.log`: root suite 121 passed, 1 skipped (one more than in the main run, the new regression test).
The main run (`release-check.log`) happened before this fix, so its start-script logs still show the error. The full release check was not repeated afterwards.

## What this does not prove
- Real household data, tariff and bill reconciliation (manual gate, not done).
- That the screens look right to a person: the Chromium tests check text, classes and behaviour, not appearance.
- Real headshot loading from formula1.com hosts (listed as a user check in `f1-setup-lab/tests/fixtures/phase1_test_report.md`).
- pandas 2.x: only pandas 3.1.1 ran.
