# F1 SETUP LAB Historical Phase 1: test report (task 10.2)

Requirement 9.5. Baseline: `phase1_baseline.json` (task 1.1, tree before any phase 1 code change).

## Environment

| | Root `.venv` | `/tmp/pd3` |
|---|---|---|
| Python | 3.12.0 | 3.12.12 |
| pandas | 2.3.3 | 3.1.1 |
| playwright | installed | not installed |

Each suite ran separately, one at a time, from its own directory.

## Results against the baseline

| Suite | Environment | Passed | Failed | Skipped | XFail | Baseline passed / failed / skipped / xfail | Change in passed |
|---|---|---|---|---|---|---|---|
| Root (`.venv/bin/python -m pytest tests -q -rs`) | root `.venv` | 106 | 0 | 1 | 0 | 106 / 0 / 1 / 0 | 0 |
| F1 (`cd f1-setup-lab && ../.venv/bin/python -m pytest tests -q -rsx`) | root `.venv` | 352 | 0 | 5 | 2 | 288 / 0 / 5 / 2 | +64 |
| WATTWISE (`cd wattwise-lab && ../.venv/bin/python -m pytest tests -q -rs`) | root `.venv` | 155 | 0 | 0 | 0 | not in baseline (restoration report: 155 / 0 / 0 / 0) | 0 |
| F1 | `/tmp/pd3` (pandas 3.1.1) | 303 | 0 | 8 | 2 | not in baseline | n/a |

No test failed in any run. The 64 extra F1 tests are the phase 1 property, route, frontend and doc tests. The root suite did not change because phase 1 added no root tests. The skip counts match the baseline on the root `.venv`.

## Environment-caused failures

None in these runs.

Two environment conditions caused skips, not failures:

1. **pandas 3 venv, 3 browser tests skipped** (`test_frontend_setup_lab.py:106`, `test_phase1_frontend.py:71`, `test_ui_openf1.py:16`).
   - Condition: `/tmp/pd3` has no `playwright`.
   - Cause: the venv was built for the pandas check only. All three run and pass in the root `.venv`. Together with the 5 live OpenF1 skips this gives 8 skips on pandas 3 against 5 on the root `.venv`.
2. **5 live OpenF1 tests skipped** (`tests/test_openf1.py:394`).
   - Condition: `RUN_NETWORK_TESTS` is not set.
   - Cause: opt-in, they contact `api.openf1.org`.

Other skips and expected failures, unchanged from the baseline:
- Root: `tests/test_start_scripts.py:106`, opt-in `RUN_START_SMOKE=1` (needs network for pip).
- F1 xfail (`strict=False`) in `tests/test_restored_scripts.py`: `test_probe_missing_recording` and `test_verify_missing_report`. Both are known requirement 8.4 message gaps in restored scripts that are byte-identical to the archive.

A `StarletteDeprecationWarning` about `httpx` vs `httpx2` appears in every run.

## Checks left to the user

1. **Live OpenF1 suite.** Run `cd f1-setup-lab && RUN_NETWORK_TESTS=1 ../.venv/bin/python -m pytest tests/test_openf1.py -q -rs`. It needs network and `api.openf1.org` availability. It was not run.
2. **Real headshot loading from formula1.com hosts.** Tests cover the load-failure fallback (coded circle) and URL cleaning, but no test fetches an image from a real host. Open an imported session in a browser and confirm the images load with `referrerpolicy="no-referrer"`. Only sessions imported after this change carry headshots (re-import with force to refresh).
3. **`scripts/check-release.sh`.** Not run in this task. Run it before release.

## Known limits

- **Tyres tab fit line.** The fitted line is drawn through the centroid of the points with the response slope, not from a stored intercept.
- **OpenF1 laps without `is_valid`.** Laps with no `is_valid` value are treated as valid, so they stay in the tyre fit.
- **Standings CSV.** The standings export is untested with real rows from the fixtures.
- **Fuel burn.** Tyre slopes do not correct for fuel burn (stated on the tab and in the READMEs).
- **Mini-sector distance.** Distance is an estimate (stated on the tab and in the READMEs).
- **Headshots and team colours.** Present only for sessions imported after this change.
- **pandas support.** The F1 suite passes on pandas 2.3.3 and 3.1.1. Other versions were not run.

## Requirement-to-test map (task 11)

Final F1 run (root `.venv`): 352 passed, 5 skipped, 2 xfailed, 0 failed.

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
