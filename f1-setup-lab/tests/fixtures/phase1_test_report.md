# F1 SETUP LAB Historical Phase 1: test report (task 10.2)

Requirement 9.5. Baseline: `phase1_baseline.json` (task 1.1, tree before any phase 1 code change).

## Environment
Refreshed on 2026-11-25/26 by a clean `scripts/check-release.sh` run (fresh release virtual environment). The root suite was re-run afterwards (121 passed) because one test was added for a start-script fix; every other count is from that run. Evidence: `proof-of-run/PROOF_OF_RUN.md`.
| | Release environment |
|---|---|
| Python | 3.13.11 |
| pandas | 3.1.1 |
| numpy / scipy | 2.5.5 / 1.18.1 |
| trialforge-core (installed wheel) | 0.2.2 |
| playwright (Chromium) | 1.65.2, installed |
| pytest / hypothesis | 9.2.0 / 6.170.17 |
Each suite ran separately, one at a time, from its own directory. The earlier two-environment comparison (pandas 2.3.3 and 3.1.1) is not repeated: the release environment is pandas 3.1.1 and pandas 2 was not re-run.
## Results against the baseline
| Suite | Passed | Failed | Skipped | XFail | Baseline passed / failed / skipped / xfail | Change in passed |
|---|---|---|---|---|---|---|
| SDK (`skeleton/packages/trialforge-core/tests`) | 263 | 0 | 0 | 0 | not in baseline | n/a |
| SDK workspace (`skeleton/tests`) | 54 | 0 | 0 | 0 | not in baseline | n/a |
| Root (`tests`) | 121 | 0 | 1 | 0 | 106 / 0 / 1 / 0 | +15 |
| F1 (`f1-setup-lab/tests`, includes the Chromium tests) | 283 | 0 | 5 | 0 | 288 / 0 / 5 / 2 | -5 |
| F1 Chromium browser tests (run separately) | 7 | 0 | 0 | 0 | not in baseline | n/a |
| WATTWISE (`wattwise-lab/tests`) | 252 | 0 | 0 | 1 | not in baseline (restoration report: 155 / 0 / 0 / 0) | n/a |
| WATTWISE Chromium browser tests (run separately) | 9 | 0 | 0 | 0 | not in baseline | n/a |
| Real OpenF1 imports, five sessions 2023-2026 (`RUN_NETWORK_TESTS=1`) | 5 | 0 | 0 | 0 | not run in baseline | n/a |
No test failed. The F1 count is 5 below the baseline of 288 because the tests of the removed recorded-stint feature (including `test_restored_scripts.py`, which held the two baseline xfails) no longer exist since migration 10, partly offset by tests added since; the removed tests were not itemised. The 5 F1 skips are the live OpenF1 tests in `tests/test_openf1.py`, which the release script runs as their own check (passed). The one root skip is the full install and start smoke in `tests/test_start_scripts.py`, which the release script also runs as its own checks (passed).
## Environment-caused failures
None in this run.
Skips, all expected:
- **5 live OpenF1 tests** in `tests/test_openf1.py:394`, opt-in (`RUN_NETWORK_TESTS`). The release script ran the real imports as a separate check and it passed.
- **1 root start smoke** in `tests/test_start_scripts.py:110`, opt-in (`RUN_START_SMOKE=1`). The release script ran the startup smoke as separate checks and they passed.
- **WATTWISE xfail** (1): a known, non-strict expected failure that is unchanged.
A `StarletteDeprecationWarning` about `httpx` vs `httpx2` appears in every run.
## Checks left to the user
1. **Real headshot loading from formula1.com hosts.** Tests cover the load-failure fallback (coded circle) and URL cleaning, but no test fetches an image from a real host. Open an imported session in a browser and confirm the images load with `referrerpolicy="no-referrer"`. Only sessions imported after the phase 1 change carry headshots (re-import with force to refresh).
2. **Real household data.** The release script reports the household meter export, tariff and bill reconciliation as a manual gate (skipped by design).
Done since the previous version of this report: the live OpenF1 suite (five real sessions) and `scripts/check-release.sh` (34 checks passed, 0 failed).
## Known limits

- **Tyres tab fit line.** The fitted line is drawn through the centroid of the points with the response slope, not from a stored intercept.
- **OpenF1 laps without `is_valid`.** Laps with no `is_valid` value are treated as valid, so they stay in the tyre fit.
- **Standings CSV.** The standings export is untested with real rows from the fixtures.
- **Fuel burn.** Tyre slopes are observed lap-time trends and do not correct for fuel burn (stated on the tab, in the report and in the READMEs).
- **Mini-sector distance.** Distance is an estimate (stated on the tab and in the READMEs).
- **Headshots and team colours.** Present only for sessions imported after this change.
- **pandas support.** The F1 suite passes on pandas 3.1.1 in the release environment. pandas 2.x was not re-run for this refresh.

## Requirement-to-test map (task 11)

Final F1 run (release environment, 2026-11-26): 283 passed, 5 skipped, 0 failed (see Results above).

| Req | Covered by | Note |
|---|---|---|
| 1 Standings, 8-entry nav | `test_phase1_frontend.py` (nav eight entries/three groups, standings with data, failed dataset, qualifying text) | 1.6 header notice and 1.7 error box rely on the restored frontend tests |
| 2 Wind direction | `test_prop_phase1_compass.py`, `test_phase1_frontend.py` (compass table, chart, gaps, all-null note) | 2.6 by `test_prop_historical_stability.py` and route manifest |
| 3 Headshots, colours | `test_prop_phase1_cleaning.py`, `test_prop_phase1_import_colour_headshot.py`, `test_phase1_frontend.py` (fallback, img, dash collision, no external host) | Real image loading is a user check (see above) |
| 4 Season comparison | `test_phase1_season_compare.py`, `test_prop_phase1_season_compare.py`, `test_phase1_routes.py` (404/422, null with reason), frontend gap test | |
| 5 Mini-sectors | `test_prop_phase1_minisectors.py`, `test_phase1_routes.py` (409 cases, ok case), frontend tab test | |
| 6 Tyre degradation | `test_prop_phase1_degradation.py`, `test_phase1_routes.py` (empty plus note), frontend tab test | Fuel and fit-line limits listed above |
| 7 Export and report | `test_prop_phase1_csv_report.py`, `test_prop_phase1_export_equals_json.py`, `test_phase1_export_routes.py`, `test_phase1_routes.py` (header-only, unavailable), frontend export test | Standings CSV with real rows untested (limit above) |
| 8 Separation, compatibility | `test_phase1_routes.py` (historical tables only), `test_prop_phase1_read_only.py`, `test_prop_historical_stability.py` (route manifest), `test_analysis_purity.py` | 8.3 not applicable: design D1 needs no Migration_10 |
| 9 Tests and verification | All files above; this report and `phase1_baseline.json` | Network use: none; live suite is a user check |

Gaps: no requirement lacks a test or a stated limit. Requirement 1.1 in `requirements.md` already reads "exactly eight", so no correction was made. The stale sentence in `design.md` (line 161, "Seven entries in three groups ... will be corrected to eight") still describes the old text and was left unchanged.
