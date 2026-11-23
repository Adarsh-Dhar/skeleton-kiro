> **Note (removed feature).** The Live Simulator (the EA SPORTS F1 25 game feature: UDP receiver, setup probing, SIM-RECORDED experiments, the `live_simulator` mode) was removed from F1 SETUP LAB. Migration 10 backs up the database, deletes the experiments and rows it recorded and drops its six tables. Setup Engineering (synthetic experiments, dashboard, new experiment, telemetry import) and Historical F1 (OpenF1) remain. The text below is the record of the earlier restoration; where it describes the recorded-stint feature (renamed here so that no current-feature wording remains), that part no longer applies.

# Requirements Document

## Introduction

F1 SETUP LAB v2.0 (`f1-setup-lab/`) is currently an OpenF1-only historical analysis app. Migration 8 in `db/migrations/versions.py` removed the setup-engineering tables and experiments, and the app no longer uses the TRIALFORGE SDK. This feature restores the setup engineering system and the recorded-stint feature from the earlier release (archive `skeleton-kiro-main (1).zip`, about 7,100 lines) and ships them as version 2.1.0 beside the existing historical features.

The restored app has three separated workspaces: Historical F1 (existing, unchanged), Setup Engineering (restored) and Recorded Stint (restored). Historical OpenF1 data stays separate from recording source and setup measurements. The synthetic simulator keeps its SYNTHETIC label. FastF1 is not restored.

Out of scope: tyre analysis, a standings screen, new report types, and any other new feature. Live packet capture cannot be tested without the recording source, so tests use recorded packets.

Audit baseline: the last test run gave 87 passed, 1 failed and 5 skipped. The failure is a pandas 3.0 string dtype round trip, probably caused by the sandbox. Wind direction is stored but is not displayed, and this feature leaves that unchanged.

## Glossary

- **F1_Setup_Lab**: The application in `f1-setup-lab/` (Python package `f1lab`, FastAPI backend, static frontend).
- **Backend**: The FastAPI service of the F1_Setup_Lab.
- **Frontend**: The static JavaScript and HTML client in `f1-setup-lab/frontend/`.
- **TRIALFORGE_SDK**: The `trialforge-core` wheel built from `skeleton/packages/trialforge-core`.
- **Root_DB**: The shared database package in `db/` and its SQLite file `data/trialforge.db`.
- **Migration_9**: The new migration that recreates the restored F1 tables and the `app_experiment_owner` table.
- **Restored_Tables**: The six tables `<recording table>`, `<packet table>`, `f1_stint_evidence`, `f1_stint_telemetry`, `f1_probe_report` and `f1_setup_mapping_confirmation`, mapped by the models F1Recording, F1Packet, F1StintEvidence, F1StintTelemetry, F1ProbeReport and F1SetupMappingConfirmation.
- **Historical_Data**: Rows in the `f1_hist_*` tables and `f1_import_job`, imported from OpenF1.
- **Setup_Measurement**: A measurement, stint evidence or stint telemetry record produced by the simulator, a telemetry import or a recording source packet recording.
- **Lab_Service**: The restored service (`services/lab`) that runs experiments, live sessions and recording source recordings.
- **Lab_Factory**: The injectable callable the Backend uses to create the Lab_Service.
- **Simulator**: The synthetic vehicle model and sim adapter (prototype, not validated physics).
- **SYNTHETIC_NOTICE**: The constant in `f1lab/__init__.py` that labels Simulator output as synthetic.
- **NOTICES**: The constant in `f1lab/__init__.py` holding the recorded-mode notices.
- **Recorded_Stint_Integration**: The package `integrations/recorded_stint` (11 files including `protocols/`) that decodes recording source packet packets and manages captures.
- **Release_Archive**: `skeleton-kiro-main (1).zip`, the source of the files to restore.
- **Workspace**: One of the three top-level navigation areas: Historical F1, Setup Engineering, Recorded Stint.
- **Release_Check**: The script `scripts/check-release.sh`.
- **Start_Script**: The script `scripts/start-f1.sh`.

## Requirements

### Requirement 1: Dependencies, version and lint configuration

**User Story:** As a maintainer, I want the F1_Setup_Lab package to depend on the TRIALFORGE_SDK again, so that the restored setup engineering code installs and lints cleanly.

#### Acceptance Criteria

1. THE F1_Setup_Lab pyproject SHALL declare exactly one dependency on `trialforge-core` with the version specifier `>=0.2.0,<0.3`.
2. THE F1_Setup_Lab pyproject SHALL declare the version string `2.1.0` and a description of 1 to 200 characters that contains the name of each of the three Workspaces.
3. THE F1_Setup_Lab pyproject SHALL NOT declare FastF1 (case-insensitive match on `fastf1`) in any dependency list, including optional dependency groups.
4. THE F1_Setup_Lab ruff configuration SHALL exempt every file matching `backend/f1lab/integrations/recorded_stint/protocols/*.py` and the file `backend/f1lab/integrations/recorded_stint/capture.py` from rule E702, and SHALL NOT exempt any other file from rule E702.
5. WHEN the Start_Script runs, THE Start_Script SHALL install the TRIALFORGE_SDK wheel into the F1_Setup_Lab environment before starting the Backend.
6. IF the TRIALFORGE_SDK wheel installation fails or the wheel is not found, THEN THE Start_Script SHALL exit with a non-zero exit code, print an error message indicating the installation failure, and not start the Backend.
7. WHEN the Release_Check runs, THE Release_Check SHALL install the TRIALFORGE_SDK wheel into the F1_Setup_Lab environment before running the F1_Setup_Lab tests.
8. IF the TRIALFORGE_SDK wheel installation fails or the wheel is not found, THEN THE Release_Check SHALL exit with a non-zero exit code, print an error message indicating the installation failure, and not run the F1_Setup_Lab tests.
9. WHEN the Start_Script or the Release_Check queries `/api/health`, THE Start_Script or Release_Check SHALL compare the `trialforge` field of the response with the installed TRIALFORGE_SDK version and treat the check as passed only if the two strings are identical.
10. IF the `trialforge` field is missing, or differs from the installed TRIALFORGE_SDK version, or `/api/health` does not return a response within 30 seconds, THEN THE Start_Script or Release_Check SHALL exit with a non-zero exit code and print an error message indicating the cause.

### Requirement 2: Restored database models

**User Story:** As a developer, I want the six setup-engineering models back in `db/models/f1.py`, so that experiments, recordings and probe evidence persist in the Root_DB.

#### Acceptance Criteria

1. THE Root_DB SHALL define the models F1Recording, F1Packet, F1StintEvidence, F1StintTelemetry, F1ProbeReport and F1SetupMappingConfirmation in `db/models/f1.py`, with table names, column names, column types, nullability, primary keys, foreign keys, unique constraints and indexes identical to those of the Release_Archive.
2. THE Root_DB SHALL keep the existing `f1_hist_dataset` model and every other existing `f1_*` model unchanged, with zero differences in table names, column definitions, constraints and indexes before and after the restoration.
3. WHEN `db/models/f1.py` is imported, THE Root_DB SHALL register all six Restored_Tables on `Base.metadata` so that each table name is present in `Base.metadata.tables`.
4. WHEN `create_tables` runs against an empty database, THE Root_DB SHALL create all six Restored_Tables by name, and the created schema SHALL match the Release_Archive schema for those tables.
5. WHEN `create_tables` runs against a database that already contains one or more Restored_Tables, THE Root_DB SHALL complete without error, preserve all existing rows in those tables, and create only the missing tables.
6. IF a Restored_Table name conflicts with a table already registered on `Base.metadata`, THEN THE Root_DB SHALL fail at import time with an error identifying the conflicting table name, and SHALL NOT create or alter any table.

### Requirement 3: Migration 9

**User Story:** As a maintainer, I want one forward migration that recreates the restored tables without touching existing data, so that existing databases upgrade safely.

#### Acceptance Criteria

1. THE Root_DB SHALL append Migration_9 as the last entry in the ordered migration list, after migration 8, without changing the SQL statements or the resulting schema of migrations 1 to 8.
2. WHEN Migration_9 runs, THE Root_DB SHALL create each of the Restored_Tables using CREATE TABLE IF NOT EXISTS semantics, so that a Restored_Table which already exists is left unmodified.
3. WHEN Migration_9 runs and the `app_experiment_owner` table is missing, THE Root_DB SHALL create the `app_experiment_owner` table.
4. WHEN Migration_9 runs on a database that already holds Historical_Data, THE Root_DB SHALL leave the row count and the column values of every row in the Historical_Data tables and the WATTWISE LAB tables identical to their values before Migration_9 ran.
5. WHEN Migration_9 runs a second time on a database where it has already been applied, THE Root_DB SHALL complete without error, make no change to the schema, and make no change to the row count or column values of any table (idempotence).
6. THE Root_DB SHALL update the docstring of migration 8 to state that migration 8 was a one-time cleanup, and the docstring of migration 4 to state that Migration_9 recreates the tables that migration 8 removed.
7. WHEN `scripts/start-f1.sh` or the database migration entry point is about to apply Migration_9 to an existing `data/trialforge.db`, THE Root_DB SHALL write a backup copy of `data/trialforge.db` under `data/backups/` in a directory or file name containing a UTC timestamp, and SHALL complete that write before the first statement of Migration_9 executes.
8. IF the backup copy cannot be written or its size differs from the size of `data/trialforge.db`, THEN THE Root_DB SHALL abort before executing any statement of Migration_9, leave `data/trialforge.db` byte-identical to its state before the attempt, report an error identifying the backup failure, and exit with a non-zero status.
9. IF any statement of Migration_9 fails while it is running, THEN THE Root_DB SHALL roll back all changes made by Migration_9, leave the database at schema version 8, and report an error identifying the failed statement.
10. THE test suite SHALL include a test that applies migrations 1 to 8 (including the data removed by migration 8), then applies Migration_9, and asserts that every Restored_Table exists and that the row count and column values of every Historical_Data table equal their values before Migration_9.
11. THE test suite SHALL update `tests/test_migration_f1_openf1_only.py` so that it passes with Migration_9 in the migration list.

### Requirement 4: Backend modules restored from the Release_Archive

**User Story:** As a developer, I want the setup engineering code restored unchanged, so that behaviour matches the previously released and tested version.

#### Acceptance Criteria

1. THE F1_Setup_Lab SHALL contain the adapters `vehicle_model`, `sim_adapter` and `telemetry`, each with content byte-identical to the corresponding file in the Release_Archive.
2. THE F1_Setup_Lab SHALL contain the catalogs `ea_f1_25` and `verification`, each with content byte-identical to the corresponding file in the Release_Archive.
3. THE F1_Setup_Lab SHALL contain the Recorded_Stint_Integration with all 11 files, including the `protocols/` package, each byte-identical to the corresponding file in the Release_Archive.
4. THE F1_Setup_Lab SHALL contain the services `lab`, `narrative` and `report_html`, each with content byte-identical to the corresponding file in the Release_Archive.
5. THE F1_Setup_Lab SHALL contain `api/schemas.py` that defines every setup schema class from the Release_Archive and every current historical schema class, with each class name defined exactly once.
6. THE `f1lab/__init__.py` module SHALL export `NOTICES` and `SYNTHETIC_NOTICE`, each with a value string-equal to the corresponding value in the Release_Archive.
7. THE F1_Setup_Lab SHALL NOT contain an `import fastf1` or `from fastf1` statement in any restored or existing Python module.
8. WHEN the Backend imports `f1lab.integrations.historical`, THE Backend SHALL load zero modules from Recorded_Stint_Integration, the Simulator or the Lab_Service, as verified by checking that none of those modules appear in `sys.modules` after the import in a fresh interpreter.
9. IF a file required by criteria 1 to 4 is missing from the Release_Archive or differs from it after restoration, THEN THE F1_Setup_Lab SHALL report a failure that names each missing or differing file, and SHALL leave all other restored files unchanged.

### Requirement 5: Setup engineering and live API routes

**User Story:** As an engineer, I want the experiment, live and recording source routes back, so that the Frontend and scripts can drive the Lab_Service.

#### Acceptance Criteria

1. THE Backend SHALL serve the restored routes (about 35): `/api/experiments*`, `/api/live/*`, `/api/<verification>/*`, `/api/telemetry/preview` and `/api/demo/{name}.csv`, and each restored route SHALL respond to a valid request with a success response (HTTP 2xx) within 2 seconds, excluding requests that start a long-running experiment or live session.
2. THE Backend SHALL create the Lab_Service through an injectable Lab_Factory so that tests can supply a Lab_Service on a temporary database, and WHERE a test supplies a Lab_Factory, THE Backend SHALL use the supplied Lab_Service for every restored route and SHALL NOT read or write the default database.
3. WHEN the Lab_Service raises a NotFound error, THE Backend SHALL respond with HTTP 404 and the error message in the response body.
4. WHEN the Lab_Service raises a Conflict error, THE Backend SHALL respond with HTTP 409 and the error message in the response body.
5. WHEN a client sends GET `/api/meta`, THE Backend SHALL return a response that lists both the historical module and the setup engineering module, each identified by name.
6. WHEN a client sends GET `/api/health`, THE Backend SHALL return the fields `trialforge` (SDK version, non-empty string) and `trialforge_path` (installed SDK location, non-empty string) in addition to the existing health fields, and THE Backend SHALL keep every existing health field name and value type unchanged.
7. THE Backend SHALL serve every `/api/historical/*` route with the same paths, methods, request schemas and response schemas as before this feature.
8. WHEN a client requests `/api/demo/{name}.csv` with a name that is not a known demo, THE Backend SHALL respond with HTTP 404 and an error message indicating the demo was not found.
9. IF a request body fails validation on a restored route, THEN THE Backend SHALL respond with HTTP 422, an error body that identifies each invalid field, and no Lab_Service state change.
10. IF the Lab_Service raises an error other than NotFound or Conflict on a restored route, THEN THE Backend SHALL respond with HTTP 500, an error message that does not include a stack trace, and no partial Lab_Service state change.

### Requirement 6: Separation of data

**User Story:** As an analyst, I want historical and setup data kept apart, so that real OpenF1 results are never mixed with simulator or recording source measurements.

#### Acceptance Criteria

1. WHEN the Lab_Service records a Setup_Measurement, THE Lab_Service SHALL write it only to the SDK tables, the Restored_Tables and `app_experiment_owner` with `app = 'f1-setup-lab'`, and SHALL write zero rows to any `f1_hist_*` table or `f1_import_job`.
2. WHEN the historical repository performs any read or write, THE historical repository SHALL access only the `f1_hist_*` tables and `f1_import_job`, and SHALL access zero SDK tables, Restored_Tables or `app_experiment_owner` rows.
3. WHEN an OpenF1 import completes, THE Backend SHALL leave the row count and content of the Restored_Tables and of every Setup_Measurement identical to their state before the import started.
4. WHEN a Setup_Measurement is recorded, THE Backend SHALL leave the row count and content of every `f1_hist_*` table identical to their state before the recording.
5. THE Backend SHALL expose zero routes that convert Historical_Data into a Setup_Measurement or a Setup_Measurement into Historical_Data.
6. WHEN the Lab_Service lists experiments, THE Lab_Service SHALL return only experiments whose `app_experiment_owner` value is `f1-setup-lab`, and SHALL return zero experiments owned by WATTWISE LAB.
7. IF an OpenF1 import fails before completion, THEN THE Backend SHALL leave the Restored_Tables and every Setup_Measurement unchanged, and SHALL record the failure in `f1_import_job` only.
8. IF a write targets a table outside the set permitted for its data type (Setup_Measurement or Historical_Data), THEN THE Backend SHALL reject the write, SHALL return an error indicating the table is not permitted, and SHALL leave all tables unchanged.

### Requirement 7: Frontend navigation and views

**User Story:** As a user, I want one app with the three Workspaces, so that I can switch between history, setup engineering and the simulator.

#### Acceptance Criteria

1. THE Frontend SHALL show exactly six navigation entries, labelled Dashboard, New experiment, Recorded stint, Historical F1, Imported sessions and Telemetry import.
2. THE Frontend SHALL group the six navigation entries under exactly three Workspace headings: Historical F1, Setup Engineering and Recorded Stint. Each entry SHALL appear under exactly one heading.
3. THE Frontend SHALL place the restored setup and live views in a new file `setup_lab.js` and keep the current `app.js` as the entry point for the historical views.
4. THE Frontend SHALL load `inspector.js` restored from the Release_Archive.
5. THE Frontend SHALL contain a merged `charts.js` that renders every chart type the historical views use and every chart type the restored setup views use.
6. WHEN a user selects a navigation entry, THE Frontend SHALL render the matching view within 1 second, excluding Backend response time.
7. WHEN a Workspace is opened, THE Frontend SHALL render its views so that the Historical F1 views show the same content and controls as before this change.
8. WHILE a Simulator view is displayed, THE Frontend SHALL show the SYNTHETIC_NOTICE text unchanged and visible without scrolling.
9. WHILE a Recorded Stint view is displayed, THE Frontend SHALL show the NOTICES recorded-mode text unchanged and visible without scrolling.
10. THE Frontend SHALL NOT add tyre analysis views, a standings screen or new report views.
11. THE Frontend SHALL NOT display wind direction in any view.
12. IF an API call fails, THEN THE Frontend SHALL show the error message returned by the Backend in the current view within 1 second of receiving the failure response, keep the current view's previously displayed data, and keep the navigation usable. IF the failure response contains no error message or the Backend is unreachable, THEN THE Frontend SHALL show a generic error message stating that the request failed.

### Requirement 8: Restored scripts

**User Story:** As an engineer, I want the probe, verification and packet inspection scripts back, so that I can verify setup mapping from the command line.

#### Acceptance Criteria

1. THE F1_Setup_Lab SHALL contain `scripts/probe_setup.py`, `scripts/verify_setup_mapping.py` and `scripts/inspect_recording.py`, each with content identical to the corresponding file in the Release_Archive except for import statements whose target modules moved, with zero other line differences.
2. THE F1_Setup_Lab SHALL keep `scripts/refresh_openf1_fixtures.py` and `scripts/import_historical.py` (OpenF1) byte-for-byte identical to their content before the restoration.
3. WHEN a restored script is run with `--help`, THE script SHALL print usage text that lists every command-line argument the script accepts and exit with status 0 within 5 seconds, without reading any input file or opening any network connection.
4. IF a restored script receives an input file path that does not exist or cannot be read, THEN THE script SHALL print to standard error an error message that contains the offending path and the reason (missing or unreadable), exit with status 1, and leave all existing files unmodified.
5. WHEN a restored script is run with valid arguments and a readable input file, THE script SHALL complete without an import error and exit with status 0.
6. IF a restored script is run with an unrecognized or missing required argument, THEN THE script SHALL print usage text and an error message to standard error and exit with a non-zero status.

### Requirement 9: Restored tests

**User Story:** As a maintainer, I want the earlier tests back and passing, so that regressions in the restored code are detected without the recording source.

#### Acceptance Criteria

1. THE test suite SHALL contain the test modules `test_api_workflow`, `test_recorded_stint`, `test_live_mode`, `test_probe_and_verification`, `test_protocol_2026`, `test_stint_measurements`, `test_telemetry_inspector`, `test_vehicle_and_telemetry`, `test_verification_scripts`, `test_csv_timestamps` and `test_notices`, plus the helper `stint_packets`, and each module SHALL collect at least 1 test.
2. THE test suite SHALL exercise live packet behaviour only with recorded packets, and no test SHALL open a network socket to, or require a process of, a running recording source.
3. THE test suite SHALL remove or rewrite every restored test that depends on FastF1, so that 0 test files contain an import of `fastf1` and 0 tests are skipped or deselected because FastF1 is missing.
4. THE test suite SHALL exclude every `test_historical*.py` file of the Release_Archive and SHALL retain every `test_historical*.py` file of the current project, with the count of current historical tests after restoration greater than or equal to the count before restoration.
5. THE test suite SHALL update `conftest.py` so that the Lab_Factory fixture and the existing OpenF1 fixtures can be requested together in a single test, with no fixture name collision and no change in the pass/fail result of any test that used the OpenF1 fixtures before the update.
6. WHEN the full F1_Setup_Lab and root test suites run in a clean environment with the SDK installed, THE test suite SHALL finish with 0 failures and 0 collection errors, except for failures caused by the environment, and each such failure SHALL be documented in the test report with the test name, the failing environment condition and the cause.
7. THE test report SHALL reproduce the pandas 3.0 string dtype round-trip failure from the audit, or SHALL state the cause with the pandas version and the failing test name.
8. IF the cause of the pandas 3.0 string dtype round-trip failure lies in project code, THEN THE F1_Setup_Lab SHALL fix it, and a test that failed before the fix SHALL pass after the fix.
9. THE test suite SHALL include a test that asserts every `/api/historical/*` route that exists in the pre-restoration route list still exists, and that each route's response has the same top-level keys and value types as before restoration.
10. THE test suite SHALL include a test that asserts the SYNTHETIC_NOTICE and each recorded-mode notice appear in the Frontend output and in the API output at every location where Requirement 7 requires them, and the test SHALL fail if any one of those locations lacks its notice.

### Requirement 10: Documentation

**User Story:** As a new user, I want accurate READMEs, so that I know what the app does and how it uses the SDK.

#### Acceptance Criteria

1. THE `f1-setup-lab/README.md` SHALL contain a section for each of the three Workspaces that gives the Workspace name and a description of its purpose of at most 100 words.
2. THE `f1-setup-lab/README.md` SHALL name the TRIALFORGE_SDK as a dependency and SHALL state the SDK's role in F1 SETUP LAB in at most 3 sentences.
3. THE `f1-setup-lab/README.md` SHALL state that all data produced by the Simulator is labelled SYNTHETIC and SHALL state that live packet capture requires the recording source to be running and sending packets.
4. THE root `README.md` SHALL describe F1 SETUP LAB as using the TRIALFORGE_SDK.
5. THE root `README.md` SHALL NOT contain any statement that the Simulator was removed and SHALL NOT contain any statement that F1 SETUP LAB does not use the TRIALFORGE_SDK.
6. THE `f1-setup-lab/README.md` and the root `README.md` SHALL each state that FastF1 is not supported and that historical data comes from OpenF1.
7. THE `f1-setup-lab/README.md` and the root `README.md` SHALL each list every live behaviour that is verified only with recorded packets, by behaviour name, and SHALL state that these behaviours have not been tested against the live recording source.
8. THE header comment of `scripts/check-release.sh` SHALL state that both applications (WattWise and F1 SETUP LAB) use the TRIALFORGE_SDK.
9. IF a README statement in `f1-setup-lab/README.md`, the root `README.md` or the `scripts/check-release.sh` header comment contradicts the current behaviour of the application or scripts, THEN THE release check SHALL be considered failed until the statement is corrected.
