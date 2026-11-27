# Proof of run: zero-fabricated-data policy (27 Nov 2026)

What was run, on what, and what came out. The raw logs are in this folder. `SHA256SUMS.txt` lists their SHA-256 hashes, so a
later edit to a log is detectable. Nothing here was edited by hand except this summary. The earlier proof
(`../PROOF_OF_RUN.md`, 25 Nov 2026) predates the data-policy work and is kept unchanged.

## Run identity

| | |
|---|---|
| Command | `TRIALFORGE_PID_DIR=<tmp> F1_PORT=18001 WATTWISE_PORT=18002 INSTALL_CHROMIUM=1 RUN_NETWORK_TESTS=1 RUN_START_SMOKE=1 scripts/check-release.sh` |
| Started | 2026-11-27T07:58:35Z (about 23 minutes) |
| Machine | macOS, arm64, Python 3.13.11 |
| SDK | trialforge-core 0.2.2, built as a wheel and installed into a fresh release environment |
| Code under test | the working tree on top of commit `1adb757` (uncommitted changes) |

The smoke check used ports 18001/18002 and a throw-away database, so the running apps and `data/trialforge.db` were not touched by it.

## Result: 34 passed, 0 failed, 3 skipped, 0 unavailable

Full table: `release-check.log`. Per-suite output: `suite-logs/`.

| Check | Result |
|---|---|
| Builds (SDK, db package, both app wheels), imports from the installed SDK | PASSED |
| Lint: root, F1, WATTWISE, SDK; Python compile; JavaScript and shell syntax | PASSED |
| SDK tests / SDK workspace tests | 263 / 54 passed |
| Root tests (database, migrations 1–12, scoping, start scripts) | 121 passed, 1 skipped |
| F1 SETUP LAB backend / browser | 282 passed, 5 skipped / 7 passed |
| **WATTWISE LAB backend** (includes `test_data_policy.py`, `test_evidence_and_audit.py`) | **315 passed, 1 xfailed** |
| **WATTWISE LAB browser (Chromium)**, from a fresh empty installation | **11 passed** |
| Real OpenF1 imports (5 sessions, live network) | PASSED |
| Startup smoke (both apps, shared database migrated, independent stop) | PASSED (7 checks) |

Skips, which are not passes:

- `tests/test_start_scripts.py:110` and F1 `tests/test_openf1.py:394` are opt-in inside pytest. The script runs both as separate checks above, and both passed.
- **Real household meter export, tariff and bill reconciliation** is a manual gate that needs the household's own data. It has not been done.

The xfail is the known, documented bug "`/api/comparison` does not expose the import's `source_label`" (strict xfail).

## Audit of the running installation

`live-database-audit.json` is `GET /api/audit` from the running WATTWISE on `data/trialforge.db` after the upgrade. It reports
**8 blocking, 2 to confirm, 0 clean**:

- The three tariffs are the old built-in examples.
- Washing machine, tumble dryer and the EV are exact copies of the old example household.
- The one experiment was built on example inputs.
- The selected tariff is an example.
- The dishwasher (1.2 kW) and the always-on load (0.4 kW) differ from the examples, so they are treated as the owner's values, to be confirmed.

**The installation does not yet contain any real input.** A backup of the database taken before the upgrade is in `data/`
(`trialforge.db.bak-before-evidence-*`).
