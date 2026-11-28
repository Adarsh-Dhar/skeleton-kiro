# Implementation Plan: F1 Unified Telemetry Import

## Overview

Add a CSV Upload provider beside OpenF1 behind one provider interface, reusing `ImportJobStore`, `HistoricalRepository`, the `f1_hist_*` tables and `analysis.py`. New code is `csv_normalizer.py` and `csv_importer.py` in `f1-setup-lab/backend/f1lab/integrations/historical/`. The Setup Engineering adapter is rewritten to call the same normalizer. Implementation language is Python (backend, pytest, hypothesis) and JavaScript (frontend), as in the design. All paths are relative to `f1-setup-lab/` unless they start with `db/`, `scripts/` or `.kiro/`. Backend code lives under `backend/f1lab/`, tests under `tests/`.

## Tasks

- [x] 1. Schema, provider interface and provenance foundation
  - [x] 1.1 Add migrations, models and write-guard entries
    - In `db/migrations/versions.py` add `_m014_f1_csv_upload` and `_m015_f1_hist_csv_metadata` (additive: `metadata_json`, `source_type`, `verification`, `file_hash` columns, backfill OpenF1 rows, `f1_hist_column_mapping` and `f1_csv_upload` tables)
    - Add `HistSession` columns, `HistColumnMapping` (cascade FK) and `CsvUpload` to the F1 models
    - Add the new `f1_hist_*` tables to `HISTORICAL_TABLES` in `db/guard.py`
    - _Requirements: 1.3, 1.6, 1.7, 8.3_

  - [x] 1.2 Create `providers.py` and route the service through it
    - Define the `HistoricalProvider` protocol, the `PROVIDERS` registry (exactly `openf1` and `csv_upload`) and an `OpenF1Provider` wrapper that leaves `import_session_openf1` unchanged
    - Change `HistoricalService._run_import` to call `PROVIDERS[payload["provider"]].run(...)`
    - Widen `HistoricalImportRequest.provider` in `f1lab/api/schemas.py` beyond `Literal["openf1"]`
    - Make `HistoricalRepository.save_session` reject a manifest missing `provider`, `source_type` or `verification` and persist them
    - Add a single `provenance_status(source)` helper returning the three exact labels, and a guard that refuses `synthetic=False` for demo sources
    - Add CSV error codes to `HistoricalImportError` (`INVALID_CSV_STRUCTURE`, `ROW_VALIDATION_FAILED`, `UPLOAD_NOT_FOUND`, `DUPLICATE_SESSION`, `DUPLICATE_KEY_CONFLICT`, `UNIT_NOT_SUPPORTED`, `METADATA_INVALID`)
    - _Requirements: 1.1, 1.2, 1.4, 1.5, 1.7, 1.8, 1.9, 9.11, 13.1, 13.2, 13.3, 13.4, 13.5_

  - [x] 1.3 Write unit tests for migrations, registry and OpenF1 regression
    - Migration applies to a database holding pre-feature data and preserves rows
    - Registry lists exactly 2 providers that implement the interface
    - OpenF1 stored records and browsing results (session list, driver list, lap list) match a stored baseline
    - Write rejected when `provider`, `source_type` or `verification` is missing
    - File: `tests/test_csv_foundation.py`
    - _Requirements: 1.1, 1.5, 1.6, 1.9, 18.3_

  - [x] 1.4 Write property test for provenance labels and flags
    - **Property 28: Provenance labels and flags are a pure function of source**
    - **Validates: Requirements 13.1, 13.2, 13.3, 13.4, 13.5, 13.6**
    - File: `tests/test_prop_csv_provenance.py`

- [x] 2. CSV normalizer
  - [x] 2.1 Implement unit conversion and timestamp parsing in `csv_normalizer.py`
    - `convert_time`, `convert_speed`, `convert_distance` with exact inverses and the constants from Requirement 5
    - Reject unsupported units with the list of supported units
    - `parse_timestamp` for ISO 8601, `YYYY-MM-DD HH:MM:SS[.fff]`, `HH:MM:SS.fff`, epoch seconds and epoch milliseconds (1e11 threshold), offset taking precedence over user timezone, returns `None` for unparseable values
    - Relative session time anchored at 0 when no offset and no timezone, timezone recorded as an Unavailable_Marker
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8, 5.12_

  - [x] 2.2 Implement lap assignment, frame builders and manifest
    - `assign_laps` (unavailable without source, verbatim valid `lap_number`, invalid values reported)
    - `build_lap_frame` and `build_telemetry_frame` producing exactly `LAP_COLUMNS` and `TELEMETRY_COLUMNS`, with unmapped channels left null and bad cells stored as missing without interpolation
    - Lap time never substituted by the sector sum
    - `build_manifest` with `provider`, `source_type`, `verification`, `column_mapping`, `units`, `file_hash`, `original_filename`, `metadata` and Data_Type_Label
    - `parse_csv(text, mapping, units, policy)` as the single shared entry point
    - _Requirements: 2.3, 2.6, 2.7, 3.4, 3.5, 3.7, 5.9, 5.10, 7.5, 8.3, 14.2_

  - [x] 2.3 Write property test for unit conversion round trip
    - **Property 9: Unit conversion round-trips within tolerance**
    - **Validates: Requirements 5.1, 5.2, 5.3, 5.11**
    - File: `tests/test_prop_csv_units.py`

  - [x] 2.4 Write property tests for timestamp parsing
    - **Property 10: Timestamp parsing is format-independent**
    - **Validates: Requirements 5.5**
    - **Property 11: Offset takes precedence over a user-supplied timezone**
    - **Validates: Requirements 5.6**
    - **Property 12: No offset and no user timezone yields relative time anchored at zero**
    - **Validates: Requirements 5.7**
    - **Property 13: Unparseable timestamps are reported and excluded, never guessed**
    - **Validates: Requirements 5.8**
    - File: `tests/test_prop_csv_timestamps.py`

  - [x] 2.5 Write property tests for unavailable channels and lap assignment
    - **Property 7: Unmapped standard fields are never populated**
    - **Validates: Requirements 3.4**
    - **Property 8: Bad or empty mapped-channel cells are stored as missing, never filled**
    - **Validates: Requirements 3.5**
    - **Property 14: Lap assignment is never inferred without a source**
    - **Validates: Requirements 5.9**
    - **Property 15: Mapped lap_number values are assigned verbatim or reported invalid**
    - **Validates: Requirements 5.10**
    - Shared generators in `tests/csv_strategies.py`; tests in `tests/test_prop_csv_channels.py`

- [x] 3. CSV importer
  - [x] 3.1 Implement inspection, classification and mapping
    - `inspect` (column names in order, row count by streaming, first 20 rows), `classify`, `suggest_mapping`, `validate_mapping` (1:1, unknown columns, previous mapping untouched on rejection)
    - Lap summary required-column check naming every missing column, and first-bad-cell row validation for lap summaries
    - _Requirements: 2.1, 2.2, 2.4, 2.5, 3.1, 3.2, 3.3, 3.6, 3.8, 4.1, 4.2, 4.3, 4.4, 4.5, 4.6_

  - [x] 3.2 Implement row validation, policies, coverage and chunked reading
    - `validate_rows`: missing counts, duplicates, out-of-order, non-numeric, percentage ranges, gear and DRS checks, skipped-time-row count
    - `drop_exact_duplicates` / `reject` and `sort` / `reject` policies with coverage-report counts
    - Blocking-error list capped at 200 with total count, coverage percentages rounded to 1 decimal
    - `read_chunks` (at most 10,000 rows per chunk), maximum file size and maximum row-count limits enforced before processing
    - _Requirements: 3.9, 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8, 6.9, 6.10, 6.11, 6.12, 12.1, 12.4, 12.5_

  - [x] 3.3 Implement `import_session_csv` and `CSVProvider`
    - Load staged upload by hash (`UPLOAD_NOT_FOUND` if absent), validate and normalize, driver filtering with imported/excluded counts, zero-match rejection
    - Session_Metadata validation of all 11 items (year, ISO date, IANA timezone, units, free-text length) with Unavailable_Markers only when explicitly marked, provider fixed to `User CSV`
    - Duplicate checks by file hash and metadata key before saving, chunk-level progress reporting at least every 5 percent, cancellation checks
    - Single `repo.save_session` call that also writes the column mapping and file provenance
    - Register `CSVProvider` in `PROVIDERS`
    - _Requirements: 1.2, 1.3, 1.8, 2.6, 2.7, 2.8, 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7, 7.8, 8.1, 8.3, 9.4, 10.1, 10.2, 10.3, 12.3, 12.7_

  - [x] 3.4 Write property tests for classification and lap-summary rules
    - **Property 1: Classification is case/whitespace-insensitive and complete**
    - **Validates: Requirements 2.1, 3.1, 3.8**
    - **Property 2: Required-column rejection names exactly the missing columns**
    - **Validates: Requirements 2.4**
    - **Property 3: Optional lap columns round-trip, blanks become null**
    - **Validates: Requirements 2.2**
    - **Property 4: Missing lap_time_s never becomes the sector sum**
    - **Validates: Requirements 2.3**
    - **Property 5: Invalid lap/sector values are rejected at the first bad cell**
    - **Validates: Requirements 2.5**
    - **Property 6: A stored Lap_Summary_CSV session has zero telemetry rows**
    - **Validates: Requirements 2.6, 2.7, 2.8**
    - File: `tests/test_prop_csv_lap_summary.py`

  - [x] 3.5 Write property tests for row validation policies
    - **Property 16: Duplicate policy outcomes are mutually exclusive and complete**
    - **Validates: Requirements 6.2, 6.7, 6.8**
    - **Property 17: Ordering policy outcomes are mutually exclusive and complete**
    - **Validates: Requirements 6.3, 6.9, 6.10**
    - **Property 18: A blocking error always leaves storage unchanged**
    - **Validates: Requirements 6.11**
    - **Property 19: Coverage percentages are internally consistent**
    - **Validates: Requirements 6.12**
    - **Property 27: File- and row-count limits reject before any row is processed**
    - **Validates: Requirements 12.4, 12.5**
    - File: `tests/test_prop_csv_validation.py`

  - [x] 3.6 Write property tests for metadata, filtering, atomicity and duplicates
    - **Property 20: Session metadata validity rules are enforced per item**
    - **Validates: Requirements 7.1, 7.3, 7.4, 7.5, 7.9**
    - **Property 21: Driver-column filtering is exact and exhaustive**
    - **Validates: Requirements 7.6, 7.7**
    - **Property 22: A failure before the commit point leaves zero rows of that import**
    - **Validates: Requirements 1.8, 1.9, 8.1, 8.2**
    - **Property 23: Stored provenance round-trips exactly**
    - **Validates: Requirements 7.8, 8.3**
    - **Property 24: Duplicate-file and duplicate-key detection is exact**
    - **Validates: Requirements 10.1, 10.2, 10.3**
    - File: `tests/test_prop_csv_import.py`

  - [x] 3.7 Write example tests for CSV import cases
    - Fixtures in `tests/csv_fixtures/`: valid lap summary, valid sampled telemetry, seconds, milliseconds, ISO 8601 with offset, epoch, UTC and a non-UTC offset, missing channel, duplicates, out-of-order, invalid unit, non-numeric value, file at the maximum size
    - Each test asserts the expected accept or reject outcome
    - File: `tests/test_csv_import_cases.py`
    - _Requirements: 18.1_

- [x] 4. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. HTTP API, lifecycle and deletion
  - [x] 5.1 Add CSV schemas and endpoints
    - Add CSV metadata, Column_Mapping, preview, validate and session-exists schemas to `f1lab/api/schemas.py`, and the `csv` sub-object on the import request
    - In `f1lab/api/app.py` add `POST /api/historical/csv/preview` (streamed, content-addressed staging in `{data_dir}/csv-uploads/`, `f1_csv_upload` row, HTTP 413 on oversize, no session or job created), `POST /api/historical/csv/validate`, `GET /api/historical/csv/session-exists`
    - Extend `POST /api/historical/import` for `provider: "csv_upload"` returning a job id within 2 seconds
    - Include provider, source_type, verification, Data_Type_Label, Provenance_Status and Session_Metadata in session summaries and manifests, with Unavailable_Markers returned as markers
    - _Requirements: 7.9, 9.1, 9.2, 11.1, 11.2, 11.8, 11.11, 11.12, 11.13, 11.14, 11.15, 12.4_

  - [x] 5.2 Implement job lifecycle, replacement, deletion and restart behaviour
    - Retry rejects with a re-upload message when the staged file is missing, new job leaves the original untouched
    - HTTP 404 for unknown job or session ids and 409 for cancel on finished jobs and retry on non-failed jobs
    - Duplicate-key pause with replace-or-cancel choice, 5-minute expiry, one-transaction replacement with rollback on failure
    - `delete_session` removes all `f1_hist_*` dependents in one transaction, rejects deletion while a job is `PROCESSING` or `SAVING`, warns and requires confirmation when linked to a TRIALFORGE experiment while keeping the measurements
    - Restart marks `QUEUED`, `PROCESSING` and `SAVING` jobs `FAILED` with reason `interrupted` and exposes no partial sessions
    - _Requirements: 8.2, 8.4, 8.5, 8.6, 8.7, 9.3, 9.5, 9.6, 9.7, 9.8, 9.9, 9.10, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8, 10.9, 10.10, 11.3, 11.4, 11.5, 11.6, 11.7, 11.9, 11.10_

  - [x] 5.3 Write property tests for replacement, deletion and preview
    - **Property 25: Session replacement and deletion are all-or-nothing**
    - **Validates: Requirements 10.5, 10.6, 10.7, 10.8**
    - **Property 26: Preview never creates a session or job**
    - **Validates: Requirements 11.1**
    - File: `tests/test_prop_csv_api.py`

  - [x] 5.4 Write integration tests for the HTTP contract and lifecycle
    - Status codes 404, 409, 413, 422, job states, progress reporting, cancellation, retry without duplicate rows, duplicate file rejection, deletion, restart persistence with identical row counts
    - Session list, driver comparison, lap selection and track chart data for a CSV session call the same `analysis.py` functions as an OpenF1 session
    - API responsiveness while an import runs, and a 1,000,000-row import within the time limit
    - File: `tests/test_csv_api_lifecycle.py`
    - _Requirements: 8.5, 9.3, 9.5, 9.9, 11.3, 11.6, 11.7, 12.2, 12.6, 17.4, 17.6, 18.2, 18.4_

- [x] 6. Setup Engineering adapter and Setup_Link
  - [x] 6.1 Rewrite `f1lab/adapters/telemetry.py` on top of the shared normalizer
    - `parse_telemetry_csv` calls `csv_normalizer.parse_csv`
    - `to_measurements` takes `provenance_status`, derives `synthetic` and `imported` from it, keeps file timestamps unmodified, marks missing timestamps unavailable, and removes the 90-second lap spacing
    - Sector-only files produce zero speed, RPM, brake and stability measurements
    - Unparseable files are rejected without changing sessions or attachments
    - _Requirements: 13.6, 14.2, 14.3, 14.4, 14.6, 14.7_

  - [x] 6.2 Split session import from experiment attachment in `f1lab/services/lab.py`
    - Separate operations for session import and explicit attach, pass the provenance status (demo versus upload) into the adapter
    - Attached run shows lap count and sector count
    - Reject measurement creation when original filename, file hash, Column_Mapping or units is unavailable, naming the field, with no partial persistence
    - _Requirements: 13.6, 13.7, 13.8, 14.1, 14.5, 18.6_

  - [x] 6.3 Implement the Setup_Link service in `f1lab/services/lab.py`
    - Selection constraints (1 driver, 1 to 100 laps, 1 to 50 metrics present for that driver and laps, 1 target experiment)
    - Factor mapping with a value and a verified statement per factor, error naming every missing factor
    - One measurement per lap and metric carrying Provenance_Status and source session id, no measurements when configuration is unknown or incomplete, `unverified_setup` quality flag excluding unverified measurements from training, full rollback on failure
    - Add the corresponding endpoint in `f1lab/api/app.py`
    - _Requirements: 15.2, 15.3, 15.4, 15.5, 15.6, 15.7, 15.8, 15.10_

  - [x] 6.4 Write property tests for the adapter
    - **Property 29: Measurement provenance fields round-trip from their source session**
    - **Validates: Requirements 13.7, 13.8**
    - **Property 30: Setup Engineering adapter output matches the shared normalizer**
    - **Validates: Requirements 14.2, 14.3, 14.4**
    - **Property 31: A sector-only file produces no derived channel measurements**
    - **Validates: Requirements 14.6**
    - File: `tests/test_prop_csv_adapter.py`

  - [x] 6.5 Write property tests for the Setup_Link
    - **Property 32: Setup_Link selection constraints are enforced**
    - **Validates: Requirements 15.2, 15.3**
    - **Property 33: Setup_Configuration completeness gates measurement creation entirely**
    - **Validates: Requirements 15.4, 15.5, 15.6**
    - **Property 34: Unverified factor mappings exclude their measurements from training**
    - **Validates: Requirements 15.7**
    - **Property 35: Setup_Link measurements carry their source session id**
    - **Validates: Requirements 15.8**
    - File: `tests/test_prop_setup_link.py`

  - [x] 6.6 Write integration tests for attachment and Setup_Link
    - Attach of imported measurements to a queued run, attach refused with no experiment change when the Setup_Configuration is unknown, rollback on failure, provenance source labels for OpenF1, user CSV and demo CSV
    - File: `tests/test_csv_setup_lab.py`
    - _Requirements: 14.1, 14.5, 15.5, 15.6, 15.10, 18.5, 18.6_

- [x] 7. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 8. Frontend
  - [x] 8.1 Add the CSV Upload workflow to `frontend/hist_picker.js`
    - Provider selector with exactly "OpenF1" (default) and "CSV Upload", OpenF1 view unchanged with no CSV controls
    - Six steps (upload, column mapping, units, metadata, validation and preview, import start) with the advance control disabled until the step is complete, values retained when going back
    - Per-channel preview graphs within 3 seconds, blocking-error list (at least first 100 plus total), start control disabled on blocking errors
    - Job polling at least every 2 seconds with state, progress and cancel, navigation to the Imported Sessions page on `COMPLETED`, failure message with retry where allowed, duplicate-session dialog with the Requirement 10 options, connection-error handling with retained values
    - _Requirements: 16.1, 16.2, 16.3, 16.4, 16.5, 16.6, 16.7, 16.8, 16.9, 16.10, 16.11, 16.12, 16.13_

  - [x] 8.2 Update the Imported Sessions page and `frontend/hist_analysis.js`
    - Provider label, Data_Type_Label and Provenance_Status badge on every row, empty and load-error states with retry
    - Same trace code path for CSV and OpenF1 sessions, lap and sector tables with all channels unavailable for `Lap summaries`, unavailable charts listing missing channels while others stay
    - "Use telemetry in Setup Experiment" action within 1 second of load, identical provenance badge label in the analysis view
    - _Requirements: 13.9, 13.10, 15.1, 17.1, 17.2, 17.3, 17.4, 17.5, 17.6, 17.7, 17.8_

  - [x] 8.3 Add the Setup_Link UI to `frontend/setup_lab.js`
    - Selection of driver, laps, metrics and target experiment, unavailable metrics not selectable, factor mapping with verified flag, error per missing factor, success count with link to the analysis view, failure message that keeps entries, provenance badge
    - _Requirements: 13.10, 14.1, 15.2, 15.3, 15.4, 15.9, 15.10_

  - [x] 8.4 Write frontend DOM tests
    - Provider selector, step gating, retained values, error list, session list badges and empty/error states, Setup_Link form validation
    - File: `tests/test_frontend_csv_import.py`
    - _Requirements: 13.9, 16.1, 16.4, 16.5, 16.7, 17.1, 17.2, 17.3_

- [x] 9. Browser tests and release evidence
  - [x] 9.1 Write the CSV browser test with OpenF1 unreachable
    - Playwright run that completes a CSV session import with OpenF1 disabled, opens the session, and saves a screenshot to the test results directory
    - File: `tests/test_ui_csv_import.py`
    - _Requirements: 18.8, 18.11_

  - [x] 9.2 Write the OpenF1 browser test with no CSV step
    - Playwright run that completes an OpenF1 import using the existing fakes without any CSV action, and saves a screenshot; passes independently of 9.1
    - File: `tests/test_ui_openf1_independent.py`
    - _Requirements: 16.2, 18.8, 18.11_

  - [x] 9.3 Wire the new suites into `scripts/check-release.sh`
    - Add named `pytest_run` entries for the CSV property suite, CSV lifecycle and regression suite, and the two browser tests, with hypothesis configured to at least 100 examples
    - Store browser screenshots or recorded output next to the test results, exit non-zero on any failure
    - _Requirements: 18.1, 18.2, 18.3, 18.4, 18.5, 18.6, 18.7, 18.8, 18.9, 18.11_

- [x] 10. Final task - Run `scripts/check-release.sh`
  - Run `scripts/check-release.sh` from the repository root and confirm exit code 0 with zero failed tests
  - Confirm screenshots or recorded output exist for both browser tests
  - Do not report the feature complete if any check fails (Requirement 18.10); fix failures and rerun
  - _Requirements: 18.9, 18.10, 18.11_

## Notes

- Tasks marked with `*` are optional test sub-tasks. Requirement 18 makes the Release_Suite tests mandatory for reporting completion, so skip them only for a fast MVP.
- The design has a Correctness Properties section, so each of Properties 1 to 35 is its own sub-task entry.
- Each task references granular requirement clauses for traceability.
- Property tests run at least 100 generated cases.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "2.1"] },
    { "id": 2, "tasks": ["2.2", "1.3", "1.4", "2.3", "2.4"] },
    { "id": 3, "tasks": ["3.1", "2.5"] },
    { "id": 4, "tasks": ["3.2"] },
    { "id": 5, "tasks": ["3.3"] },
    { "id": 6, "tasks": ["3.4", "3.5", "3.6", "3.7", "5.1", "6.1"] },
    { "id": 7, "tasks": ["5.2", "6.2"] },
    { "id": 8, "tasks": ["5.3", "5.4", "6.3", "6.4"] },
    { "id": 9, "tasks": ["6.5", "6.6", "8.1"] },
    { "id": 10, "tasks": ["8.2"] },
    { "id": 11, "tasks": ["8.3"] },
    { "id": 12, "tasks": ["8.4", "9.1", "9.2"] },
    { "id": 13, "tasks": ["9.3"] }
  ]
}
```
