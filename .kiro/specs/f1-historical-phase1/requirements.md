# Requirements Document

## Introduction

This feature adds the Phase 1 historical analysis features to the Historical F1 workspace of F1 SETUP LAB (`f1-setup-lab/`, version 2.1.0). It builds on the finished `f1-setup-lab-restoration` spec. All features read OpenF1 data that is already imported into the `f1_hist_*` tables, or that a re-import can add. None of them write to setup tables, and none of them show simulator data.

Features, in scope:

1. Championship standings screen (drivers and teams).
2. Wind direction display on the weather view.
3. Driver headshots and team colours.
4. Cross-race and season comparison.
5. Mini-sector analysis.
6. Tyre degradation analysis (historical, from stints and laps).
7. Historical report and CSV export.

Not in scope: new data providers, FastF1, changes to the Setup Engineering workspace, and live OpenF1 network tests or browser tests on the user's MacBook (listed under "Verification limits").

### Supersession of the restoration spec

The restoration spec forbids some of these features. This spec replaces those rules, and the tests that enforce them must change with it:

- Restoration requirement 7.11 forbids wind direction, tyre views, standings views and new report views in the Frontend. This spec allows exactly the views listed here and nothing else.
- Restoration "Out of scope" lists tyre analysis, a standings screen and new report types. This spec adds them.
- `tests/test_frontend_setup_lab.py` scans the frontend for `wind_direction`, tyre, standings and report views and pins the router and tab keys. It must be updated to the new allowed set.

### Starting state (verified in the code)

- `/api/historical/sessions/{id}/datasets/{name}` serves the stored datasets, including `drivers_championship` and `teams_championship`. No screen shows them.
- `wind_direction_deg` is stored in the weather table and is returned by the weather route. The weather view does not show it.
- Laps carry `stint`, `compound`, `tyre_life`, `is_valid` and `pits`. No analysis uses them.
- `POST /api/historical/sessions/{id}/compare` compares drivers inside one session only.
- Headshot and team colour: the OpenF1 drivers fixtures contain `headshot_url` and `team_colour`, but the import drops them. They belong in the stored driver list (session manifest JSON), so no migration is needed (design D1). Sessions imported earlier have no values until re-imported.
- The championship endpoints are OpenF1 beta endpoints and may be empty or missing for a session. Coverage entries already record this.

## Glossary

- **Backend**, **Frontend**, **Root_DB**, **Historical_Data**: as defined in the restoration spec.
- **Historical_Workspace**: The Historical F1 navigation area.
- **Session**: One imported OpenF1 session stored in `f1_hist_session`.
- **Coverage_Entry**: The per-endpoint record in a session manifest (rows, status, note).
- **Standings_View**: The new screen showing drivers and teams championship tables.
- **Team_Colour**: A six-digit hex colour provided by OpenF1 for a team.
- **Headshot**: A driver image URL provided by OpenF1.
- **Stint_Degradation**: The change in lap time per lap of tyre age within one stint, fuel effect not corrected.
- **Mini_Sector**: One of N equal-distance slices of a lap computed from stored telemetry distance.
- **Export_File**: A CSV or HTML file generated from stored Historical_Data.

## Requirements

### Requirement 1: Championship standings screen

**User Story:** As an analyst, I want to see drivers and teams championship standings for an imported race, so that I can place a race result in its season context.

#### Acceptance Criteria

1. THE Frontend SHALL add a Standings entry under the Historical F1 heading, and the navigation SHALL then list exactly seven entries in two groups (Setup Engineering 3, Historical F1 4: Import from OpenF1, Imported sessions, Standings, Season comparison).
2. WHEN the user opens a race or sprint Session in the Standings_View, THE Frontend SHALL show a drivers table (position, driver, team, points, points gained in this session when provided) and a teams table (position, team, points), both ordered by position ascending.
3. THE Standings_View SHALL read only `datasets/drivers_championship` and `datasets/teams_championship`, and SHALL NOT call any import route.
4. IF the Session is not a race or sprint, THEN THE Standings_View SHALL show the text "Standings are published for race and sprint sessions only" and no table.
5. IF the Coverage_Entry for a championship dataset has status other than `ok` or has zero rows, THEN THE Standings_View SHALL show the Coverage_Entry note and status in place of that table, and the other table SHALL still render if it has rows.
6. THE Standings_View SHALL show the historical notice in the fixed header and SHALL NOT show the synthetic or live-simulator notice.
7. WHEN the Backend returns an error, THE Standings_View SHALL show the backend message in the error box and keep the navigation usable.

### Requirement 2: Wind direction display

**User Story:** As an analyst, I want to see wind direction beside wind speed, so that I can judge the weather on track.

#### Acceptance Criteria

1. THE weather view SHALL show wind direction in degrees (0 to 360) and as a compass point (N, NE, E, SE, S, SW, W, NW) for each weather sample that has `wind_direction_deg`.
2. THE weather view SHALL include a wind direction line or arrow series on the existing weather chart, drawn with the existing chart helpers.
3. IF `wind_direction_deg` is null for a sample, THEN THE weather view SHALL show "no data" for that sample and SHALL NOT show a value of 0.
4. IF every sample lacks `wind_direction_deg`, THEN THE weather view SHALL show one note "wind direction not published for this session" and SHALL NOT draw an empty series.
5. THE conversion from degrees to compass point SHALL use eight 45-degree sectors centred on N=0, so 337.5 up to but excluding 22.5 is N, and 360 maps to N.
6. THE feature SHALL NOT change the stored values, the weather route response keys or the weather table.

### Requirement 3: Driver headshots and team colours

**User Story:** As an analyst, I want driver photos and team colours in tables and charts, so that I can tell drivers and teams apart quickly.

#### Acceptance Criteria

1. THE design SHALL first confirm, from the OpenF1 drivers fixtures and `openf1_normalizer.py`, whether `headshot_url` and `team_colour` are fetched and stored. IF either is missing, THEN the design SHALL add it to the import and to the session drivers response, and Migration_10 SHALL add any new column without changing existing rows.
2. THE drivers response of `GET /api/historical/sessions/{id}/drivers` SHALL include `team_colour` (six hex digits or null) and `headshot_url` (an `https` URL or null) for every driver, and SHALL keep all existing keys and types.
3. THE Backend SHALL accept a `team_colour` only if it matches `^[0-9A-Fa-f]{6}$` and a `headshot_url` only if it is an `https` URL of at most 500 characters; any other value SHALL be stored as null.
4. THE Frontend SHALL use the Team_Colour for that team's chart lines, tables chips and track-map dots in the Historical_Workspace, and SHALL fall back to the existing palette when null.
5. IF two drivers in one chart have the same Team_Colour, THEN THE Frontend SHALL distinguish them by line dash pattern or marker shape, and SHALL NOT draw two identical lines.
6. THE Frontend SHALL show the Headshot next to the driver name in the driver picker and standings tables, with a fixed size of 32 px.
7. IF a Headshot fails to load or is null, THEN THE Frontend SHALL show the driver's three-letter code in a circle in the Team_Colour (or neutral grey) and SHALL NOT show a broken-image icon.
8. THE Frontend SHALL load Headshots with `referrerpolicy="no-referrer"` and SHALL NOT send any request to a host other than the stored URL's host.
9. Team_Colour SHALL NOT be the only carrier of meaning: every coloured element SHALL also have a text label.

### Requirement 4: Cross-race and season comparison

**User Story:** As an analyst, I want to compare drivers across several imported sessions, so that I can see form over a season.

#### Acceptance Criteria

1. THE Backend SHALL provide `POST /api/historical/compare-sessions` taking 2 to 24 stored session ids, a list of drivers (1 to 6 driver codes), and a metric chosen from `best_lap`, `median_lap`, `finish_position`, `grid_position`, `points`.
2. THE response SHALL contain, for each requested driver and session, the metric value or null, plus the session label (year, event, session type), ordered by session start time ascending.
3. IF a driver did not take part in a session, or the dataset needed for the metric is missing, THEN THE value SHALL be null and the response SHALL include a reason string for that cell, and SHALL NOT use 0 or a neighbouring value.
4. IF a session id is not stored, THEN THE Backend SHALL respond 404 naming the id. IF the request has fewer than 2 or more than 24 sessions, or an unknown metric, THEN THE Backend SHALL respond 422 naming the field.
5. THE Backend SHALL treat each driver as identified by driver code and SHALL NOT merge two different drivers who share a code in different seasons without the response marking `driver_numbers` per session.
6. THE Backend SHALL compute `best_lap` and `median_lap` from valid, non-deleted laps only, and SHALL NOT include in-laps or out-laps flagged by `pits`.
7. THE comparison route SHALL be read-only and SHALL NOT change any table.
8. THE Frontend SHALL add a Season comparison view under the Historical F1 heading with a session multi-select, a driver multi-select, a metric selector, and a line chart with one point per session and gaps for null cells.
9. THE Frontend SHALL show the reason string for a null cell on hover and in a text list below the chart.
10. THE Season comparison view SHALL NOT mix sessions of different session types in one series unless the user selects them explicitly, and SHALL show the session type in each point label.

### Requirement 5: Mini-sector analysis

**User Story:** As an analyst, I want to see where on the lap one driver gains time on another, so that I can find the corners that matter.

#### Acceptance Criteria

1. THE Backend SHALL provide `GET /api/historical/sessions/{id}/mini-sectors` taking `driver`, an optional `lap`, an optional reference `ref_driver` and `ref_lap`, and `count` (integer 10 to 50, default 25).
2. THE Backend SHALL build Mini_Sectors by dividing the lap's `distance_m` range into `count` equal slices and SHALL compute for each slice the time spent (from `session_time_s`) by interpolating at the slice boundaries.
3. WHEN a reference is given, THE response SHALL include for each slice the time delta (driver minus reference, seconds, positive means slower) and the cumulative delta at the slice end.
4. IF telemetry for the driver or lap is missing, or `distance_m` or `session_time_s` is not strictly increasing over at least 90 percent of the samples, THEN THE Backend SHALL respond 409 with a message saying which channel failed, and SHALL NOT return interpolated invented values.
5. IF the lap is omitted, THEN THE Backend SHALL use the driver's fastest valid lap and SHALL return the lap number used.
6. THE sum of the slice times SHALL equal the lap time from the telemetry within 0.05 seconds, and the response SHALL include both numbers.
7. THE Frontend SHALL add a Mini-sectors tab to the session view that shows a bar per slice coloured by gain or loss against the reference, with a text value on each bar, and the track map shaded by slice when position data exists.
8. THE route SHALL be read-only.

### Requirement 6: Tyre degradation analysis

**User Story:** As an analyst, I want to see how lap times change as tyres age, so that I can compare compounds and stints.

#### Acceptance Criteria

1. THE Backend SHALL provide `GET /api/historical/sessions/{id}/tyre-degradation` returning, per driver and stint, the compound, first and last tyre age, number of laps used, the degradation slope in seconds per lap, and the fit's R squared.
2. THE Backend SHALL fit the slope by least squares of lap time against `tyre_life` over valid, non-deleted laps that are not in-laps, out-laps, or under a safety car or virtual safety car where race control data identifies those laps, and SHALL list the number of laps excluded and why.
3. IF a stint has fewer than 5 usable laps, THEN THE slope and R squared SHALL be null with the reason "fewer than 5 usable laps".
4. THE response SHALL state that fuel burn is not corrected and that a negative slope can result from falling fuel load.
5. THE response SHALL include a per-compound summary (median slope over stints with a slope) and SHALL NOT mix compounds in one summary row.
6. IF the session has no stint or compound data, THEN THE Backend SHALL respond 200 with an empty list and the Coverage_Entry note, and SHALL NOT respond 500.
7. THE Frontend SHALL add a Tyres tab to the session view with a scatter of lap time against tyre age per stint, the fitted line, and a table of the response rows, and SHALL label the fuel caveat on the tab.
8. THE existing "Weather & tyres" tab and "Tyres & pit stops" table SHALL remain unchanged in behaviour.
9. THE route SHALL be read-only.

### Requirement 7: Historical report and CSV export

**User Story:** As an analyst, I want to download a session's results and analyses, so that I can share or process them outside the app.

#### Acceptance Criteria

1. THE Backend SHALL provide `GET /api/historical/sessions/{id}/export.csv?table=<name>` for `laps`, `results`, `standings`, `weather`, `tyre_degradation` and `mini_sectors` (the last with the same query parameters as Requirement 5), returning `text/csv; charset=utf-8` with a header row and a `Content-Disposition` attachment filename containing the session id and table name.
2. THE CSV writer SHALL escape fields that contain commas, quotes or newlines per RFC 4180, and SHALL prefix any text cell that begins with `=`, `+`, `-` or `@` with a single quote to prevent spreadsheet formula injection.
3. THE CSV values SHALL equal the values the matching JSON route returns for the same session, with null written as an empty cell.
4. THE Backend SHALL provide `GET /api/historical/sessions/{id}/report.html` returning one self-contained HTML document (no external scripts, styles or images) with the session summary, results, standings if present, tyre degradation table, weather summary and the historical notice and data source (OpenF1).
5. THE report SHALL HTML-escape every value taken from stored data.
6. IF a requested table is unknown, THEN THE Backend SHALL respond 422 naming the allowed tables. IF the session does not exist, THEN THE Backend SHALL respond 404 naming the id.
7. IF a section has no data, THEN THE report and CSV SHALL include the section with an "unavailable" note (report) or a header-only file (CSV), and SHALL NOT omit the section silently.
8. THE export routes SHALL be read-only, and the report SHALL NOT contain SYNTHETIC or simulator content.
9. THE Frontend SHALL add Export buttons to the session view that download the CSV tables and open the report.

### Requirement 8: Data separation and compatibility

**User Story:** As a maintainer, I want these features to leave the existing apps and data untouched, so that the restoration stays valid.

#### Acceptance Criteria

1. THE new routes SHALL read only `f1_hist_*` tables and `f1_import_job`, and the data-separation write guard SHALL remain installed on the historical engine.
2. THE existing 21 historical routes SHALL keep their paths, methods, request and response schemas, and top-level keys and types, except that the drivers response MAY gain `team_colour` and `headshot_url` keys. The route manifest `historical_routes.json` SHALL be regenerated and committed in the same change, and the diff SHALL show only added routes and those two added keys.
3. IF a migration is needed (Requirement 3.1), THEN Migration_10 SHALL be idempotent, SHALL preserve existing rows, SHALL take the pre-migration backup defined for Migration_9, and SHALL roll back fully on failure.
4. THE Frontend SHALL NOT call any external host except Headshot image URLs.
5. THE new features SHALL NOT import from `adapters` or `services`.
6. THE existing test suites SHALL pass, with the tests named under "Supersession" updated and no other test weakened.

### Requirement 9: Tests and verification

**User Story:** As a maintainer, I want the new features tested against fixtures, so that I can trust them without the live API.

#### Acceptance Criteria

1. THE tests SHALL use the OpenF1 fixtures in `tests/fixtures/openf1` and SHALL NOT use the network.
2. THE test suite SHALL include property tests (at least 100 examples unless stated) for: compass conversion (Requirement 2.5), CSV escaping and formula-injection prefix (7.2), mini-sector slice times summing to lap time (5.6), degradation slope recovery on generated linear data (6.2), and export values equal to JSON values (7.3).
3. THE test suite SHALL include a test that every new route leaves a snapshot of all tables unchanged.
4. THE test suite SHALL include Frontend tests for navigation (eight entries, three groups), the standings screen states, wind direction states, Headshot fallback and colour collision handling.
5. THE restoration test baseline file SHALL be extended with the new counts, and the test report SHALL record results and any environment-caused failure.

## Verification limits

These cannot be verified in this workspace and are left to the user's machine: the live OpenF1 suite (`RUN_NETWORK_TESTS=1`), real headshot image loading from OpenF1 hosts, and the root release script (`scripts/check-release.sh`) that needs network access.
