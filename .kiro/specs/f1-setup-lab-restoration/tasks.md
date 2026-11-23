# Implementation Plan: F1 SETUP LAB Restoration (v2.1.0)

## Overview

The design is Python (FastAPI, SQLAlchemy, SQLite) with a static JavaScript frontend and shell scripts, so all tasks use those. The work restores the setup engineering system and live simulator from the Release_Archive (`~/Downloads/skeleton-kiro-main (1).zip`, unzipped to `/tmp/rel/`, cross-checked against git commit `dfa519e`). It starts by freezing the current behaviour (golden files and baselines), then adds the database layer, restores and merges the backend, restores tests, merges the frontend, updates scripts and READMEs, and ends with a full run and report.

Paths are relative to the workspace root. `f1-setup-lab/` is the app, `db/` is the Root_DB package, and `scripts/` holds the shell scripts. Restored archive files must stay byte-identical (design section "Restore plan by area"). Property tests use Hypothesis, each tagged `# Feature: f1-setup-lab-restoration, Property N: <text>`, with at least 100 examples except where the design allows fewer (Properties 3, 4, 5 and 12).

## Tasks

- [x] 1. Baselines and golden files (before any code change)
  - [x] 1.1 Generate and commit the historical route manifest
    - Write a generator script (for example `f1-setup-lab/scripts/gen_historical_manifest.py`) that loads the current, unmodified app
    - Record every `/api/historical/*` route with method, path, request schema and response schema
    - For each GET route and each OpenF1 fixture session, record top-level response keys and value types
    - Save to `f1-setup-lab/tests/fixtures/historical_routes.json`
    - _Requirements: 5.7, 9.9_

  - [x] 1.2 Generate and commit schema golden files
    - `f1-setup-lab/tests/fixtures/schema_pre_restoration.json`: `PRAGMA table_info`, `index_list` and `foreign_key_list` for every table of the current database at version 8 (all `f1_*` tables included)
    - `f1-setup-lab/tests/fixtures/restored_schema.json`: the same output for the six Restored_Tables, produced by applying the archive's own migration 4 to an empty database
    - _Requirements: 2.1, 2.2, 2.4, 3.1_

  - [x] 1.3 Add Hypothesis to the test extras
    - Add `hypothesis>=6` to the test extras in the root `pyproject.toml` and `f1-setup-lab/pyproject.toml`
    - Do not change any other pyproject field in this task
    - _Requirements: 9.6_

  - [x] 1.4 Build the restoration manifest from the archive
    - Compute sha256 for every file to restore: adapters, catalogs, `game_udp` (11 files), services, `api/schemas.py`, `inspector.js`, the three scripts, and the restored test modules and `udp_packets.py`
    - Cross-check each hash against `dfa519e`
    - Save to `f1-setup-lab/tests/fixtures/restoration_manifest.json`
    - Record the expected hashes of `scripts/refresh_openf1_fixtures.py` and `scripts/import_historical.py` from the current tree
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.9, 8.1, 8.2_

  - [x] 1.5 Record the pre-change test baseline
    - Run the root and F1 test suites on the unmodified tree
    - Record pass, fail and skip counts, the count of current historical tests, and Python, pandas and SDK versions in a baseline file under `f1-setup-lab/tests/fixtures/`
    - List the 5 opt-in skipped tests and their reasons
    - _Requirements: 9.4, 9.6, 9.7_

- [x] 2. Database layer: models, migration 9, backup, write guard
  - [x] 2.1 Append the six restored models to `db/models/f1.py`
    - Copy `UdpRecording`, `UdpPacket`, `StintEvidence`, `StintTelemetrySample`, `ProbeReport` and `SetupMappingConfirmation` from the archive, keeping the archive class names (design D1)
    - Add the `LargeBinary` and `UniqueConstraint` imports
    - Leave every existing model unchanged
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6_

  - [x] 2.2 Add migration 9 and update docstrings in `db/migrations/versions.py`
    - Append `_m009_f1_setup_restored` creating `app_experiment_owner` and the six tables, parents first, with `checkfirst` semantics
    - Wrap table creation so a failure names the table being created
    - Do not alter migrations 1 to 8 other than docstrings: migration 8 is a one-time cleanup, migration 4 notes that migration 9 recreates the removed tables
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.9_

  - [x] 2.3 Implement the pre-migration backup
    - Create `db/backup.py` with `BackupError`, `backup_database` (SQLite online backup API on a read-only connection, temp file then rename, size check) and `backup_hook`
    - Backup path: `data/backups/<YYYYmmddTHHMMSSZ>/trialforge.before-migration-9.db`
    - Add the optional `before_apply` argument to `migrate` in `db/migrations/runner.py`, called before each pending migration's transaction
    - Convert `BackupError` to `MigrationError`
    - Make `db/migrate.py` `main` print the backup error and return exit code 1
    - Skip the backup for fresh, empty or non-file databases and when the version is already 9 or higher
    - _Requirements: 3.7, 3.8_

  - [x] 2.4 Implement the data-separation write guard
    - Create `db/guard.py` with `PERMITTED` sets for the `historical` and `setup` domains, `TableNotPermitted` and `install_guard`
    - List the SDK experiment tables from the SDK source and confirm which engine the SDK repository uses (design risk note); document any bypass in the module docstring
    - The guard checks INSERT, UPDATE, DELETE and REPLACE targets and leaves reads unrestricted
    - _Requirements: 6.1, 6.2, 6.8_

  - [x] 2.5 Write property test for migration 9 data preservation
    - **Property 1: Migration 9 preserves existing data and creates what is missing**
    - **Validates: Requirements 2.5, 3.2, 3.3, 3.4**

  - [x] 2.6 Write property test for migration 9 idempotence
    - **Property 2: Migration 9 is idempotent**
    - **Validates: Requirements 3.5**

  - [x] 2.7 Write property test for migration 9 rollback
    - **Property 3: A failing statement in migration 9 rolls everything back**
    - **Validates: Requirements 3.9**

  - [x] 2.8 Write property test for backup before migration
    - **Property 4: Backup precedes migration 9 and equals the source**
    - **Validates: Requirements 3.7**

  - [x] 2.9 Write property test for failed backup abort
    - **Property 5: A failed backup aborts the migration without changing the database**
    - **Validates: Requirements 3.8**

  - [x] 2.10 Write property test for the write guard
    - **Property 14: The write guard rejects tables outside the domain**
    - **Validates: Requirements 6.8**

  - [x] 2.11 Write unit tests for models and migration 9
    - Compare the schema built by `create_tables` and by migration 9 with `restored_schema.json`, and all other tables with `schema_pre_restoration.json`
    - Assert `create_tables` on a database that already has some restored tables keeps rows and creates only the missing ones
    - Assert a duplicate table name raises at import and names the table
    - Assert the migration list ends with 9 and the docstring changes
    - Add the test that applies migrations 1 to 8, then 9, and compares Historical_Data rows and restored table existence
    - Update `tests/test_migration_f1_openf1_only.py` for Migration_9 (latest version becomes 9)
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 3.1, 3.6, 3.10, 3.11_

- [x] 3. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Restore and merge the backend
  - [x] 4.1 Copy the unchanged modules from the archive
    - `adapters/` (`vehicle_model`, `sim_adapter`, `telemetry` and `__init__`), `catalogs/` (`ea_f1_25`, `verification`, `__init__`), `services/` (`lab`, `narrative`, `report_html`, `__init__`), `integrations/game_udp/` (all 11 files with `protocols/`) and `api/schemas.py`
    - Files must hash-match the restoration manifest
    - Confirm `integrations/historical/__init__.py` imports nothing from the setup packages and that no module imports `fastf1`
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.7, 4.8_

  - [x] 4.2 Merge `f1lab/__init__.py`
    - Version `2.1.0`, three-workspace docstring without FastF1 text
    - `SYNTHETIC_NOTICE` and `SIM_RECORDED_NOTICE` byte-identical to the archive
    - Keep the current OpenF1 `HISTORICAL_NOTICE`
    - Export `NOTICES` with keys `synthetic`, `live_simulator`, `historical`; keep `notice_for`
    - _Requirements: 4.6, 7.8, 7.9_

  - [x] 4.3 Update `f1-setup-lab/pyproject.toml`
    - Version `2.1.0`, description under 200 characters naming the three workspaces
    - Exactly one `trialforge-core>=0.2.0,<0.3` dependency, no `fastf1` anywhere
    - Per-file E702 ignores for `backend/f1lab/integrations/game_udp/protocols/*.py` and `.../game_udp/capture.py` only
    - _Requirements: 1.1, 1.2, 1.3, 1.4_

  - [x] 4.4 Merge `api/app.py`
    - Signature `create_app(db_path, frontend_dir, openf1_client_factory, lab_factory)`; drop `fastf1_client_factory`
    - Run `migrate(engine, before_apply=backup_hook)`; build the lab through `lab_factory` when supplied, else `LabService(db_path)`; set `app.state.lab`, `historical`, `engine`
    - Copy the 33 restored routes from the archive; keep the 21 current historical routes and `/api/frontend-build`; do not copy the archive's FastF1 historical routes
    - Register exception handlers per the design table: both NotFound and Conflict class pairs, SDK errors, 422 per-field bodies, and a catch-all 500 with a fixed message and no traceback (logged server-side)
    - Unknown `/api/demo/{name}.csv` returns 404 with a demo-not-found message
    - Merge `/api/meta` (archive keys, current keys, `modules` list) and `/api/health` (existing fields plus `trialforge`, `trialforge_path`, `recovered`)
    - No route converts between Historical_Data and Setup_Measurement
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8, 5.9, 5.10, 6.5_

  - [x] 4.5 Wire the write guard into the Backend
    - Install the `historical` guard on the historical service engine and the `setup` guard on engines the Lab_Service path exposes, from `api/app.py`
    - Do not edit `services/lab.py`; if the SDK bypasses the SQLAlchemy engine, record the limit in a code comment and rely on the snapshot properties
    - Leave `db.migrate` unguarded
    - _Requirements: 6.1, 6.2, 6.8_

  - [x] 4.6 Write property test for domain error mapping
    - **Property 6: Domain errors map to their HTTP status and carry their message**
    - Use a stub Lab_Service through `lab_factory`
    - **Validates: Requirements 5.3, 5.4**

  - [x] 4.7 Write property test for unexpected errors
    - **Property 7: Unexpected errors give a 500 without a stack trace**
    - Use `TestClient(app, raise_server_exceptions=False)`
    - **Validates: Requirements 5.10**

  - [x] 4.8 Write property test for invalid experiment requests
    - **Property 8: Invalid experiment requests are rejected naming every invalid field**
    - **Validates: Requirements 5.9**

  - [x] 4.9 Write property test for unknown demo names
    - **Property 9: Unknown demo names give 404**
    - **Validates: Requirements 5.8**

  - [x] 4.10 Write property test for historical route stability
    - **Property 10: Historical routes and their responses are stable**
    - Compare with `historical_routes.json`
    - **Validates: Requirements 5.7, 9.9**

  - [x] 4.11 Write property test for setup writes
    - **Property 11: Setup writes touch only setup tables**
    - **Validates: Requirements 6.1, 6.4**

  - [x] 4.12 Write property test for historical imports
    - **Property 12: Historical imports touch only historical tables, even on failure**
    - Use OpenF1 fixtures and failure injection; the example count may be lower than 100, with the reason in the test
    - **Validates: Requirements 6.2, 6.3, 6.7**

  - [x] 4.13 Write property test for experiment scoping
    - **Property 13: Experiment listing is scoped to F1 SETUP LAB**
    - **Validates: Requirements 6.6**

  - [x] 4.14 Write property test for UDP packet round trip
    - **Property 15: Recorded UDP packets round-trip**
    - Cover `RawRecorder`/`read_raw` and `write_raw_file`/`read_raw_file`
    - **Validates: Requirements 9.2**

  - [x] 4.15 Write unit and integration tests for the backend
    - Manifest comparison test that names every missing or differing file, plus a tampered-copy test of the checker
    - AST tests: unique schema class names across `api/schemas.py` and `integrations/historical/schemas.py`, no `fastf1` import, `NOTICES` equal to the expected values
    - Subprocess test: importing `f1lab.integrations.historical` loads no game_udp, adapter or lab module
    - pyproject parse test (1.1 to 1.4)
    - Each restored route returns 2xx within 2 seconds with a stub lab; `lab_factory` isolation (no default database touched)
    - `/api/meta` and `/api/health` key and type checks; route-list test for 6.5
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9, 5.1, 5.2, 5.5, 5.6, 6.5_

- [x] 5. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Restore and update tests
  - [x] 6.1 Restore the archive test modules and merge `conftest.py`
    - Copy `test_api_workflow`, `test_game_udp`, `test_live_mode`, `test_probe_and_verification`, `test_protocol_2026`, `test_stint_measurements`, `test_telemetry_inspector`, `test_vehicle_and_telemetry`, `test_csv_timestamps` and `udp_packets.py`
    - Rewrite `test_notices.py` for the OpenF1 historical notice (design D2)
    - Restore `test_verification_scripts.py` without the `smoke_fastf1_import` tests
    - Merge `conftest.py`: one `db_path`, a `lab_factory` fixture and a `client` fixture, without breaking the OpenF1 fixtures; do not restore the archive `test_historical*.py` or `test_ui_browser.py`
    - Ensure no test file imports `fastf1` and none is skipped for a missing FastF1
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5_

  - [x] 6.2 Update existing tests that contradict the feature
    - `tests/test_app_scoping.py` (F1 now has experiments), and any db test that asserts version 8 as latest other than the one handled in 2.11
    - Do not edit `tests/test_start_scripts.py` here (handled in 9.4)
    - _Requirements: 3.11, 9.4, 9.6_

  - [x] 6.3 Write the notice-location test
    - Assert the SYNTHETIC_NOTICE and each game-mode notice appear in API output and in the Frontend files at each location Requirement 7 requires; the test fails if any location lacks its notice
    - _Requirements: 9.10, 7.8, 7.9_

- [x] 7. Merge the frontend
  - [x] 7.1 Merge `frontend/charts.js`
    - Keep the current implementations; add `scatter` and `bars` from the archive
    - Diff `lines` and `sectors` between the two versions and keep the superset of options so both callers render
    - _Requirements: 7.5_

  - [x] 7.2 Create `setup_lab.js` and restore `inspector.js`
    - Cut archive `app.js` lines 22 to 575 into `setup_lab.js` (setup and live views, telemetry import, chart helpers)
    - Remove redeclarations of `esc`, `fmt`, `$`, `view`, `META`, `ApiError`, `api`, `toast`, `errorsBox`, `setNav`, `setLed`, `downsample`
    - Copy `inspector.js` unchanged from the archive
    - Do not bring in the archive's FastF1 historical block
    - _Requirements: 7.3, 7.4_

  - [x] 7.3 Update `index.html`, `app.js` and `style.css`
    - Six nav entries under three headings (Setup Engineering: Dashboard, New experiment, Telemetry import; Live Simulator: Live simulator; Historical F1: Historical F1, Imported sessions)
    - Script order `charts.js`, `inspector.js`, `hist_race.js`, `hist_picker.js`, `setup_lab.js`, `app.js`; router dispatches setup and historical routes; historical views read `META.notices.historical`
    - Render the synthetic and game-mode notices in the fixed header so they are visible without scrolling
    - Views render into a nested container so an API failure shows the Backend message (or a generic `request failed`) and keeps earlier data and navigation
    - No tyre, standings or new report views; no wind direction in any view
    - _Requirements: 7.1, 7.2, 7.6, 7.7, 7.8, 7.9, 7.10, 7.11, 7.12_

  - [x] 7.4 Write frontend tests
    - `node --check` on each script; static parse of `index.html` for six entries in three groups; scan for `wind_direction`, tyre, standings and report views
    - Opt-in Playwright tests (same marker as `test_ui_openf1.py`): navigation timing, historical view regression, notices in the viewport, API failure message, one chart of each type
    - _Requirements: 7.1, 7.2, 7.5, 7.6, 7.7, 7.8, 7.9, 7.10, 7.11, 7.12_

- [x] 8. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 9. Scripts and release check
  - [x] 9.1 Restore the three scripts
    - Copy `scripts/probe_setup.py`, `scripts/verify_setup_mapping.py` and `scripts/inspect_udp_recording.py` from the archive, changing only import statements whose modules moved
    - Leave `refresh_openf1_fixtures.py` and `import_historical.py` untouched
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6_

  - [x] 9.2 Update `scripts/start-f1.sh` and `scripts/lib/common.sh`
    - Correct the `start-f1.sh` header comment
    - Make sure the SDK wheel is installed before the Backend starts and that a missing wheel or failed install exits 1 with a message
    - Add the post-start `/api/health` check to `run_app` (30 s, `HEALTH_TIMEOUT`): compare `trialforge` with the installed SDK version; on mismatch, missing field or no response print the cause, stop the child process and exit 1
    - _Requirements: 1.5, 1.6, 1.9, 1.10_

  - [x] 9.3 Update `scripts/check-release.sh`
    - Header comment states both applications use the TRIALFORGE_SDK
    - Install the SDK wheel before F1 tests; fail with a message if it is missing or the install fails
    - `health_ok` compares the SDK version for both apps and fails on a missing `trialforge`
    - Replace the "F1 does not use the SDK" assertions (lines 218 to 237) with the WATTWISE-style SDK assertions and add a check that `fastf1` is not in the F1 requirements
    - _Requirements: 1.7, 1.8, 1.9, 1.10, 10.8_

  - [x] 9.4 Write tests for the start script and release check
    - Update `tests/test_start_scripts.py`: replace the `sdk: not used by F1 SETUP LAB` assertion with the wheel report
    - Cases: empty wheel directory exits 1 with no listening port; a fake `/api/health` server for matching, differing, missing-field and no-response cases
    - _Requirements: 1.5, 1.6, 1.7, 1.8, 1.9, 1.10_

  - [x] 9.5 Write tests for the restored scripts
    - Subprocess tests: `--help` (exit 0, lists every argument, under 5 seconds, no network), missing or unreadable input (exit 1, path and reason on stderr, files unmodified), unknown or missing argument (usage, non-zero), one run with a recorded fixture (exit 0)
    - Note any archive script that does not meet 8.4 as a recorded gap, since 8.1 forbids other edits
    - _Requirements: 8.3, 8.4, 8.5, 8.6_

- [x] 10. Documentation
  - [x] 10.1 Update `f1-setup-lab/README.md` and the root `README.md`
    - F1 README: one section per workspace (at most 100 words each), SDK named as a dependency with its role in at most 3 sentences, simulator data labelled SYNTHETIC, live UDP capture needs the running game
    - Both READMEs: F1 SETUP LAB uses the TRIALFORGE_SDK, FastF1 is not supported and history comes from OpenF1, and a list by name of live behaviours verified only with recorded packets, stated as not tested against the live game
    - Remove any statement that the Simulator was removed or that F1 does not use the SDK
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.9_

  - [x] 10.2 Write documentation tests
    - Substring and word-count checks on both READMEs and the `check-release.sh` header; forbidden-phrase checks
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8, 10.9_

- [x] 11. Full run and report
  - [x] 11.1 Run the full suites and write the test report
    - Run root and F1 suites in a clean environment with the SDK installed; confirm 0 failures and 0 collection errors, and that each of the 11 modules plus `udp_packets` collects at least one test
    - Run the F1 suite in a venv with `pandas>=3`; record the result of the string dtype round-trip test, or state the cause with the pandas version and failing test name (design D9)
    - Document each environment-caused failure with test name, condition and cause, list the 5 opt-in skips (D10), and compare the historical test count with the task 1.5 baseline
    - _Requirements: 9.4, 9.6, 9.7_

  - [x] 11.2 Fix the pandas 3.0 round-trip failure if it lies in project code
    - Only if 11.1 shows a project-code cause: fix it and confirm the previously failing test passes
    - Otherwise record in the report that no code change was needed
    - _Requirements: 9.8_

- [x] 12. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP.
- Task 1 must land before any other change, because the golden files must describe current behaviour.
- Restored files must remain byte-identical to the archive; only the merged files (`__init__.py`, `app.py`, `charts.js`, `app.js`, `index.html`, `conftest.py`, `pyproject.toml`, `db/models/f1.py`) are edited by hand.
- Live UDP behaviour is verified with recorded packets only.
- Open items from the design: the glossary model names (D1) and the `NOTICES["historical"]` deviation (D2) should be corrected in requirements.md.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2", "1.3", "1.4"] },
    { "id": 1, "tasks": ["1.5"] },
    { "id": 2, "tasks": ["2.1", "2.3", "2.4"] },
    { "id": 3, "tasks": ["2.2", "4.1", "4.2", "4.3"] },
    { "id": 4, "tasks": ["2.5", "2.6", "2.7", "2.8", "2.9", "2.10", "2.11", "4.4", "7.1", "7.2", "9.1", "9.2"] },
    { "id": 5, "tasks": ["4.5", "6.1", "7.3", "9.3"] },
    { "id": 6, "tasks": ["4.6", "4.7", "4.8", "4.9", "4.10", "4.11", "4.12", "4.13", "4.14", "4.15", "6.2", "6.3", "7.4", "9.5", "10.1"] },
    { "id": 7, "tasks": ["9.4", "10.2"] },
    { "id": 8, "tasks": ["11.1"] },
    { "id": 9, "tasks": ["11.2"] }
  ]
}
```
