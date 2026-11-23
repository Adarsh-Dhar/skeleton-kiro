# Implementation Plan: F1 SETUP LAB Historical Phase 1

## Overview

Python (FastAPI, pandas, SQLite) backend and a static JavaScript frontend, so all tasks use those. The plan first freezes the current behaviour and adds the pure analysis module with its property tests, then adds the import and route changes, then the frontend, then updates the tests that the restoration spec made contradictory, and ends with a full run and report.

Paths are relative to the workspace root. `f1-setup-lab/` is the app. The pure module is `f1-setup-lab/backend/f1lab/integrations/historical/analysis.py` and may import only `numpy`, `pandas`, `csv`, `io`, `html`, `math`, `statistics` and the standard library. All new routes are read-only. No migration is needed (design D1). Property tests use Hypothesis, tagged `# Feature: f1-historical-phase1, Property N: <text>`, with at least 100 examples unless stated. Run tests one suite at a time with the root `.venv` from `f1-setup-lab`.

## Tasks

- [x] 1. Baseline before any code change
  - [x] 1.1 Record the current test counts
    - Run the root and F1 suites and write pass, fail and skip counts and the pandas, Python and SDK versions to `f1-setup-lab/tests/fixtures/phase1_baseline.json`
    - _Requirements: 9.5_

  - [x] 1.2 Confirm wind direction and rainfall flag survive storage
    - Import an OpenF1 fixture session with weather and read it back through the repository and the weather route
    - Record whether `wind_direction_deg` and `rainfall_flag` are present with their dtypes
    - If either is dropped, fix the repository in this task (smallest change, no schema change) before any frontend work
    - _Requirements: 2.1, 2.6_

- [x] 2. Pure analysis module
  - [x] 2.1 Create `analysis.py` with compass, cleaning and CSV helpers
    - `compass(deg)` per design D6, `clean_colour` (six hex digits, upper-cased) and `clean_url` (https, host present, at most 500 characters), both returning `None` on invalid input and never raising
    - `csv_text(header, rows)` per design D10: RFC 4180 escaping, `'` prefix for string cells starting with `=`, `+`, `-` or `@`, numbers unchanged, null as empty
    - _Requirements: 2.5, 3.3, 7.2_

  - [x] 2.2 Add mini-sector computation
    - `mini_sectors(telemetry, count, ref=None)` per design D7: equal slices of the lap's distance range, `numpy.interp` of `session_time_s`, per-slice time, normalised-distance comparison with a reference, per-slice and cumulative delta
    - Raise `Conflict` (from `f1lab.errors`) naming the failing channel when distance or time is not strictly increasing over at least 90 percent of samples
    - Return the telemetry lap time and the sum of slices
    - _Requirements: 5.2, 5.3, 5.4, 5.5, 5.6_

  - [x] 2.3 Add tyre degradation computation
    - `tyre_degradation(laps, race_control)` per design D8: filter invalid, deleted, pit, out-lap and safety-car or VSC laps, least-squares fit of lap time against `tyre_life`, R squared, per-stint exclusion counts, per-compound median slope, caveat text
    - Slope and R squared are `None` with the reason "fewer than 5 usable laps" when fewer than 5 laps remain
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5_

  - [x] 2.4 Add season comparison and report builders
    - `season_compare(sessions, drivers, metric)` per design D9: one cell per driver per session in date order, a value or a reason string, `driver_numbers` per session
    - `report_html(...)` per design D11: one escaped document, inline style, no script, no external resources, an "unavailable" note for each empty section
    - _Requirements: 4.1, 4.2, 4.3, 4.5, 4.6, 7.4, 7.5, 7.7_

  - [x] 2.5 Write property test for compass sectors
    - **Property 1: Compass sector**
    - `compass(d)` equals the sector of `d mod 360`, `compass(d) == compass(d + 360)`, boundaries 22.5 and 337.5 as in D6
    - Put the same table of cases in a shared JSON fixture that the frontend test also reads
    - **Validates: Requirements 2.5**

  - [x] 2.6 Write property test for colour and URL cleaning
    - **Property 2: Colour and URL cleaning**
    - For any string the results are valid or `None` and never raise
    - **Validates: Requirements 3.3**

  - [x] 2.7 Write property tests for mini-sectors
    - **Property 3: Mini-sector sum**, **Property 4: Reference antisymmetry**, **Property 5: Rejection of unusable telemetry**
    - **Validates: Requirements 5.2, 5.3, 5.4, 5.6**

  - [x] 2.8 Write property tests for tyre degradation
    - **Property 6: Degradation recovery** (noise at most 0.02 s, slope within 0.05, exact with zero noise, null with fewer than 5 laps)
    - **Property 7: Degradation exclusions** (adding invalid, deleted, pit or safety-car laps with extreme times does not change the slope)
    - **Validates: Requirements 6.2, 6.3**

  - [x] 2.9 Write property test for season comparison shape
    - **Property 8: Season comparison shape**
    - One cell per driver per session in date order, each cell has a value or a reason and never both or neither
    - **Validates: Requirements 4.2, 4.3**

  - [x] 2.10 Write property tests for CSV and report
    - **Property 9: CSV safety and round trip**
    - **Property 11: Report escaping** (no unescaped `<`, `>`, `&` or quotes from data, no `<script`)
    - **Validates: Requirements 7.2, 7.5**

  - [x] 2.11 Write a static test that `analysis.py` is pure
    - Parse its imports with `ast`; allow only the modules named in the Overview
    - Fail if it imports `adapters`, `services`, the service, the repository or FastAPI
    - _Requirements: 8.5_

- [x] 3. Checkpoint - Ensure all tests pass
  - Run the F1 suite and the root suite; ask the user if questions arise

- [x] 4. Import and drivers response (headshots and team colours)
  - [x] 4.1 Keep `team_colour` and `headshot_url` in the driver list
    - In `openf1_normalizer.driver_list` add both keys, cleaned with `clean_colour` and `clean_url`
    - `drivers_preview` uses `driver_list`, so the preview carries them too
    - _Requirements: 3.1, 3.2, 3.3_

  - [x] 4.2 Return the keys for every stored session
    - In `HistoricalService.drivers`, fill missing `team_colour` and `headshot_url` with `None` so sessions imported earlier return the same shape
    - Keep every existing key and type
    - _Requirements: 3.2, 8.2_

  - [x] 4.3 Write property test for the import keeping colour and headshot
    - **Property 14: Import keeps colour and headshot**
    - For the driver rows of each OpenF1 fixture, `driver_list` returns the cleaned values; invalid values become `None`
    - Plus a unit test that an old-format manifest (no keys) returns `None` for both
    - **Validates: Requirements 3.1, 3.2**

- [x] 5. Service methods and routes
  - [x] 5.1 Add tyre degradation and mini-sector service methods and routes
    - `GET /api/historical/sessions/{id}/tyre-degradation`: 404 for an unknown session, 200 with `stints: []` and a `coverage_note` when no stint data exists
    - `GET /api/historical/sessions/{id}/mini-sectors`: parameters `driver`, optional `lap`, optional `ref_driver` and `ref_lap`, `count` 10 to 50 (default 25); 404 session, 422 bad driver or `count`, 409 unusable telemetry; the fastest valid lap when `lap` is omitted, with the lap number returned
    - Use `NotFound`, `Conflict` and `ValueError` so the existing handlers give 404, 409 and 422
    - _Requirements: 5.1, 5.5, 5.8, 6.1, 6.6, 6.9_

  - [x] 5.2 Add the season comparison method and route
    - Add `SeasonCompareRequest` to `integrations/historical/schemas.py` with a class name unique across both schema modules
    - `POST /api/historical/compare-sessions`: 2 to 24 sessions, 1 to 6 drivers, metric from the five allowed; 404 naming an unknown session, 422 naming the field
    - Read results, grid and championship datasets from the repository
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7_

  - [x] 5.3 Add the export routes
    - `GET /api/historical/sessions/{id}/export.csv?table=` for `laps`, `results`, `standings`, `weather`, `tyre_degradation`, `mini_sectors` (mini-sector query parameters as in 5.1), `text/csv; charset=utf-8`, attachment filename with session id and table name; 422 naming allowed tables, 404 for an unknown session
    - `GET /api/historical/sessions/{id}/report.html`: one self-contained document with summary, results, standings, tyre degradation, weather summary (wind as compass) and the historical notice
    - Each CSV table is built from the same service method as its JSON route
    - _Requirements: 7.1, 7.3, 7.4, 7.6, 7.7, 7.8_

  - [x] 5.4 Regenerate the route manifest
    - Run `f1-setup-lab/scripts/gen_historical_manifest.py` and commit the result in the same change
    - Check that the diff contains only the new routes and the two added driver keys; stop and report any other difference
    - _Requirements: 8.2_

  - [x] 5.5 Write property test for export equals JSON
    - **Property 10: Export equals JSON**
    - For each of the six tables on the fixture sessions, the CSV cells equal the JSON route values (null as empty)
    - **Validates: Requirements 7.3**

  - [x] 5.6 Write property test for read-only routes
    - **Property 12: Read-only**
    - Call every new route in random order with random valid parameters; a snapshot of all tables is identical before and after
    - **Validates: Requirements 4.7, 5.8, 6.9, 7.8, 8.1**

  - [x] 5.7 Write unit tests for the routes
    - Use the five fixtures; no network
    - Cover every row of the design's error table: 404, 422 and 409 cases, empty tyre data, missing championship dataset, telemetry with decreasing distance, an unknown table name, a driver absent from a session
    - Check that new routes read only `f1_hist_*` tables and `f1_import_job`
    - _Requirements: 4.4, 5.4, 6.6, 7.6, 7.7, 8.1, 9.1, 9.3_

- [x] 6. Checkpoint - Ensure all tests pass
  - Run the F1 suite, the root suite and `gen_historical_manifest.py --check`; ask the user if questions arise

- [x] 7. Frontend
  - [x] 7.1 Create `hist_analysis.js` with the shared helpers
    - `compass`, `teamColour`, `distinguish` (dash patterns when two series share a colour), `avatar` (32 px image with `referrerpolicy="no-referrer"`, coded circle on error or null)
    - Reuse `esc`, `api`, `view` and `Charts`; do not redeclare globals; pass `node --check`
    - _Requirements: 2.1, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9_

  - [x] 7.2 Add the standings view
    - `#/standings`: a picker of stored race and sprint sessions, a drivers table (position, headshot, code, team, points, gained) and a teams table, reading only the two championship datasets
    - Qualifying or practice shows the fixed text; a dataset that is not `ok` or has zero rows shows the coverage note and status while the other table still renders
    - Historical notice only; backend errors in the error box
    - _Requirements: 1.2, 1.3, 1.4, 1.5, 1.6, 1.7_

  - [x] 7.3 Add wind direction to the weather tab
    - A wind direction chart and a table column with degrees and compass point; null samples drop out of the line and show "no data"; all-null shows one note and no chart
    - Do not change any other weather chart
    - _Requirements: 2.1, 2.2, 2.3, 2.4_

  - [x] 7.4 Add the Mini-sectors and Tyres tabs
    - Mini-sectors: driver and reference pickers, a bar per slice coloured by gain or loss with a text value, track map shaded by slice when position data exists, the estimated-distance note, a 409 message shown as the backend text
    - Tyres: scatter of lap time against tyre age per stint with the fitted line, a table of the response, the fuel caveat on the tab
    - Keep the existing "Weather & tyres" tab and table unchanged
    - _Requirements: 5.7, 6.7, 6.8_

  - [x] 7.5 Add the Season comparison view and export buttons
    - `#/season`: session multi-select (from the sessions list), driver multi-select, metric selector, a line chart with a point per session and gaps for null cells, reasons in a list under the chart, session type in each point label, no mixing of session types unless chosen
    - Export buttons in the session header download the six CSV tables and open the report
    - _Requirements: 4.8, 4.9, 4.10, 7.9_

  - [x] 7.6 Apply team colours across historical views
    - Use `teamColour` for chart lines, table chips and track-map dots in the Historical workspace; fall back to the existing palette when null; add the driver code to every legend and table so meaning is not colour-only
    - _Requirements: 3.4, 3.5, 3.9_

  - [x] 7.7 Update `index.html`, `app.js` and `style.css`
    - Navigation of seven entries in two groups: Setup Engineering 3, Historical F1 4 (Import from OpenF1, Imported sessions, Standings, Season comparison)
    - Script order `charts.js, inspector.js, hist_race.js, hist_picker.js, setup_lab.js, hist_analysis.js, app.js`
    - Router entries for `standings` and `season`, tab entries `minisectors` and `tyres`, `showNotice('historical')` on every new view
    - _Requirements: 1.1, 1.6, 8.4_

- [x] 8. Update tests that the restoration spec made contradictory
  - [x] 8.1 Update the existing frontend and backend tests
    - `tests/test_frontend_setup_lab.py`: allowed router heads and tab keys, script order, eight nav entries; remove only the "wind direction", tyre and standings forbidden-word scans that the new features replace and keep the other scans
    - `tests/test_ui_openf1.py`: navigation count and order
    - `tests/test_backend_restoration.py`: the route list and the "no route converts between historical and setup data" check (new routes are all historical)
    - `tests/test_prop_historical_stability.py` and the regenerated manifest: confirm they pass with the added routes and keys
    - Do not weaken any other assertion
    - _Requirements: 8.2, 8.6_

  - [x] 8.2 Write frontend tests
    - Static: `node --check` on all scripts, eight entries in three groups, script order, no external host except the headshot image
    - Playwright (same gate as `test_ui_openf1.py`): standings with data, with a failed dataset and for a qualifying session; wind chart with data, partial nulls and all nulls; headshot load failure shows the coded circle; two drivers with one colour get different dashes; season comparison with a null cell and its reason; mini-sector and tyre tabs render with fixture data; export buttons download a file
    - Compare `compass` in JS with the shared case table from task 2.5
    - _Requirements: 9.4_

- [x] 9. Checkpoint - Ensure all tests pass
  - Run the F1 suite and the root suite one at a time; ask the user if questions arise

- [x] 10. Documentation, full run and report
  - [x] 10.1 Update the READMEs
    - Add the seven features to the Historical F1 section of `f1-setup-lab/README.md` and the root `README.md`
    - State that headshots and team colours appear only for sessions imported after this change (re-import with force to refresh), that tyre slopes do not correct for fuel burn, and that mini-sector distance is an estimate
    - Keep the doc tests in `tests/test_docs_restoration.py` passing; update only assertions the new text legitimately changes
    - _Requirements: 8.6_

  - [x] 10.2 Run the full suites and write the test report
    - Run root, F1 and WATTWISE suites one at a time with the root `.venv`; run the F1 suite on pandas 3 in `/tmp/pd3` if it still exists
    - Write `f1-setup-lab/tests/fixtures/phase1_test_report.md` with counts against `phase1_baseline.json`, each environment-caused failure (name, condition, cause), and the checks left to the user: live OpenF1 suite (`RUN_NETWORK_TESTS=1`), real headshot loading from formula1.com hosts, and `scripts/check-release.sh`
    - _Requirements: 9.5_

- [x] 11. Final checkpoint - Ensure all tests pass
  - Confirm every requirement 1 to 9 has at least one passing test or a stated limit in the report; ask the user if questions arise

## Notes

- Each task names the requirements it covers. Property tests cover design properties 1 to 12 and 14. Property 13 (route stability) is covered by tasks 5.4 and 8.1.
- Checkpoints ensure incremental validation.
- Execute tasks 2.1 to 2.4 before their property tests, task 4 before the drivers part of task 7, and task 5 before the frontend views that call the routes.
