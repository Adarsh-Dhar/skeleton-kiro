# F1 SETUP LAB restoration: test report (task 11.1)

Requirements 9.4, 9.6, 9.7. Baseline: `test_baseline.json` (task 1.5).

## Environment

| | Root `.venv` | Fresh venv `/tmp/pd3` (D9) |
|---|---|---|
| Python | 3.12.0 | 3.12 (Homebrew) |
| pandas | 2.3.3 | 3.1.1 |
| numpy | 2.5.5 | 2.5.5 |
| trialforge-core (SDK) | 0.2.1 | 0.2.1 (from `skeleton/dist/trialforge_core-0.2.1-py3-none-any.whl`) |
| trialforge-db | editable, workspace root | editable, workspace root |
| f1-setup-lab | editable | editable |

The fresh venv had network access (pip). It does not have `playwright`, so 2 browser tests skip there.

## Results

Each suite ran separately, from its own directory, to avoid module-name collisions (`tests/`, `conftest.py`).

| Suite | Command | Passed | Failed | Skipped | XFail | Collection errors |
|---|---|---|---|---|---|---|
| Root | `.venv/bin/python -m pytest tests -q -rs` | 106 | 0 | 1 | 0 | 0 |
| F1 (root `.venv`) | `cd f1-setup-lab && ../.venv/bin/python -m pytest tests -q -rsx` | 288 | 0 | 5 | 2 | 0 |
| WATTWISE | `cd wattwise-lab && ../.venv/bin/python -m pytest tests -q -rs` | 155 | 0 | 0 | 0 | 0 |
| F1 (pandas 3.1.1 venv) | same, `/tmp/pd3/bin/python` | 262 | 1 | 7 | 2 | 0 |

The root suite's 1 skip is `tests/test_start_scripts.py:106` (opt-in `RUN_START_SMOKE=1`, needs network for pip).

No source change was needed. The `from db import migrate` shadowing hazard did not cause a failure in any single-process run.

## Environment-caused failures

1. **`tests/test_historical_repository.py::test_dtypes_nulls_and_extra_columns_round_trip`**
   - Condition: pandas >= 3 (seen on 3.1.1). Passes on pandas 2.3.3.
   - Failure: `assert 'object' == 'str'` for column `driver` (line 45). The test builds `laps` with string columns that pandas 3 types as the new `str` dtype. `repo.laps()` returns those columns as `object`.
   - Cause: the repository's read path does not restore the `str` dtype. Pandas 3 changed the default string dtype, and the test compares `str(dtype)` for every column. This is the audit's pandas 3.0 failure from design D9, now reproduced.
   - Status: not fixed (outside task 11.1's scope; the supported pandas in the root `.venv` is 2.3.3). `f1-setup-lab/pyproject.toml` allows `pandas>=2`, so a fix in the repository (or a pin to `<3`) is a follow-up decision.
2. **`tests/test_probe_and_verification.py::test_probe_cli_over_real_udp_stores_report_and_recording`** (flaky, not reproduced)
   - Condition: failed once (`no_change_observed` instead of `isolated_change_observed`) when the F1 suite ran at the same time as the WATTWISE suite.
   - Cause: the test starts `probe_setup.py` as a subprocess and sleeps a fixed 1.0 s before sending UDP packets. Under CPU load the probe's socket was not ready and the packets were lost. It passed alone and in two further F1 runs (the clean run in the table above).
   - Status: timing sensitivity only. Run the suites one at a time.

## Opt-in skips (5, `RUN_NETWORK_TESTS=1`)

All in `tests/test_openf1.py:393`, contacting `api.openf1.org`:
- `test_live_openf1_import[2023-Bahrain-R]`
- `test_live_openf1_import[2024-Monza-Q]`
- `test_live_openf1_import[2024-Miami-S]`
- `test_live_openf1_import[2025-Abu Dhabi-R]`
- `test_live_openf1_import[2026-Paulo-R]`

None is skipped for a missing FastF1. The pandas 3 venv also skips 2 playwright browser tests (`test_frontend_setup_lab.py:118`, `test_ui_openf1.py:16`) because `playwright` was not installed there; they run in the root `.venv`.

## Historical test count vs baseline

`tests/test_historical_repository.py` collects **6** tests. The baseline in `test_baseline.json` is 6. No change. Baseline OpenF1 modules also match: `test_openf1.py` 61, `test_openf1_fixtures.py` 25, `test_ui_openf1.py` 7, `test_frontend_build.py` 1.

## Restored modules collect at least one test

F1 collected 295 tests in total (288 passed, 5 skipped, 2 xfailed). Per module:

| Module | Tests |
|---|---|
| test_api_workflow | 42 |
| test_game_udp | 20 |
| test_live_mode | 21 |
| test_probe_and_verification | 7 |
| test_protocol_2026 | 5 |
| test_stint_measurements | 3 |
| test_telemetry_inspector | 5 |
| test_vehicle_and_telemetry | 13 |
| test_csv_timestamps | 2 |
| test_notices (rewritten) | 5 |
| test_verification_scripts | 2 |

That is the 11 restored modules, each with at least 1 test. `udp_packets.py` is a helper with no tests of its own; it is imported by `test_protocol_2026.py`, `test_restored_scripts.py` and `test_prop_udp_roundtrip.py`, which collect (5, 17 and 1 tests). No test file imports `fastf1`.

## Known 8.4 script gaps (xfail, `strict=False`, `tests/test_restored_scripts.py`)

1. `test_probe_missing_recording` (line 114): `probe_setup.py --from-recording <missing id>` exits via `SystemExit(str)` with "has no packets". The message names the id but not "missing" or "unreadable" as 8.4 requires.
2. `test_verify_missing_report` (line 124): `verify_setup_mapping.py show <missing id>` prints `no report <id>` and exits 1, with no missing or unreadable reason.

The restored scripts are byte-identical to the archive, so the gaps are recorded and not fixed.

## game_udp file count: 12 vs 11

The spec text says `integrations/game_udp/` has 11 files. The directory and `restoration_manifest.json` hold **12** `.py` files: `__init__`, `capture`, `evidence`, `live`, `measurements`, `probe`, `receiver`, `recorder`, `telemetry`, and `protocols/{__init__, f1_25, f1_25_2026}`. All 12 are restored and hash-checked; "11" in the spec is a miscount.

## Notes

- Root suite grew from 52 passed (baseline) to 106 and F1 from 95 to 288 passed, because the restoration tasks added tests since task 1.5.
- A `StarletteDeprecationWarning` about `httpx` vs `httpx2` appears in every run.

## pandas 3.1.1 round-trip failure (task 11.2, Req 9.8)
**Cause: project code.** `db/frames.py::_restore` had no branch for the pandas 3 `str` dtype. `schema_of` stored `"str"` for text columns, and `_restore` fell through to `astype(object)`, so `driver` came back as `object`.
**Fix:** `_restore` now returns `s.astype("str")` when the stored dtype is `"str"`. Under pandas 2.3.3 the stored dtype is `"object"`, so that path is unchanged. Stored data and schema format are unchanged.
**Test adjustment:** `test_dtypes_nulls_and_extra_columns_round_trip` asserted `is None` for missing text values. Under pandas 3 the `str` dtype represents missing values as NaN, so the test now uses `pd.isna` for those cells.
**Results:** `test_historical_repository.py` passes in /tmp/pd3 (pandas 3.1.1, 6 passed). Root .venv (pandas 2.3.3): F1 suite 288 passed, 5 skipped, 2 xfailed. Root suite 106 passed, 1 skipped. `gen_historical_manifest.py --check` reports the manifest is up to date.
