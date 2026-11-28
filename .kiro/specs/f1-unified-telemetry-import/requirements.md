# Requirements Document

## Introduction

F1 SETUP LAB has two separate telemetry paths today. The Historical F1 importer (OpenF1) is session-based: the user selects a year, Grand Prix and session, previews, imports with progress and coverage, and browses laps, telemetry, track position and weather. The Setup Engineering CSV path (`f1lab/adapters/telemetry.py`, `f1lab/services/lab.py`) is experiment-based: it understands only lap and sector summaries, assumes a 90-second spacing between laps instead of recovering timestamps from the file, and marks every upload `synthetic=False` and `imported=True`.

This feature adds a second provider, CSV Upload, to the existing Historical import system, so the F1 Data Import System has two providers (OpenF1 and CSV Upload) that share one pipeline: provider-specific parsing, common telemetry normalisation (units, timestamps, drivers, laps, channels, provenance), the existing historical session store (`HistoricalRepository` and the `f1_hist_*` tables), the existing historical analysis (telemetry, laps, tracks, comparisons), and an optional link to Setup Engineering (TRIALFORGE).

The OpenF1 importer and the historical analysis code are reused and not duplicated. New code lives in `f1lab/integrations/historical/csv_importer.py` and `csv_normalizer.py`. Existing OpenF1 behaviour is preserved. This spec is consistent with `f1-historical-phase1` (session store, jobs, analysis) and `f1-setup-lab-restoration` (Setup Lab attachment and provenance).

## Glossary

- **Import_System**: The unified F1 Data Import System in `f1lab/integrations/historical`, including both providers, the job store and the HTTP endpoints that expose them.
- **OpenF1_Provider**: The existing provider that imports sessions from the OpenF1 service. Its identifier is `openf1` with `source_type` `external_provider`.
- **CSV_Provider**: The new provider that imports user-supplied CSV files. Its identifier is `csv_upload` with `source_type` `user_uploaded` and `verification` `unverified`.
- **CSV_Importer**: The component in `csv_importer.py` that reads, inspects and validates CSV files and drives the CSV import workflow.
- **CSV_Normalizer**: The component in `csv_normalizer.py` that converts mapped CSV columns into standard telemetry fields, units, timestamps and laps.
- **Lap_Summary_CSV**: A CSV with columns `lap`, `sector_1_s`, `sector_2_s`, `sector_3_s` and optional `lap_time_s`, `driver`, `sector_4_s`.
- **Sampled_Telemetry_CSV**: A CSV with one row per sample, with a time column (`timestamp` or `session_time_s`) and any of `lap_number`, `distance_m`, `speed_kph`, `throttle_pct`, `brake_pct`, `steer_pct`, `gear`, `rpm`, `drs`, plus additional mapped channels the Import_System supports.
- **Standard_Field**: A named telemetry field of the historical store (for example `session_time_s`, `distance_m`, `speed_kph`, `throttle_pct`, `brake_pct`, `steer_pct`, `gear`, `rpm`, `drs`, `lap_number`).
- **Column_Mapping**: The user-confirmed assignment of CSV column names to Standard_Fields.
- **Session_Metadata**: Provider, year, Grand Prix or event name, circuit, session type, driver, session date, timezone, original filename, time unit and distance unit for an imported session.
- **Unavailable_Marker**: An explicit value recording that a metadata item or channel is not available. It is distinct from an empty string, zero or a default.
- **Import_Job**: A persisted record of one import, with state `QUEUED`, `PROCESSING`, `SAVING`, `COMPLETED`, `FAILED` or `CANCELLED`.
- **Historical_Repository**: The existing `HistoricalRepository`, which stores sessions in `f1_hist_session`, `f1_hist_frame`, `f1_hist_lap`, `f1_hist_telemetry`, `f1_hist_position`, `f1_hist_weather` and `f1_hist_dataset`.
- **Session_API**: The existing historical session browsing endpoints, shared by both providers.
- **Data_Type_Label**: The label shown for a session's content, either `Full telemetry` or `Lap summaries`.
- **Provenance_Status**: One of `OPENF1 — external provider`, `USER UPLOADED — UNVERIFIED` or `SYNTHETIC DEMO — generated test data`.
- **Historical_Picker**: The frontend module `frontend/hist_picker.js`.
- **Historical_Analysis_View**: The frontend module `frontend/hist_analysis.js`.
- **Imported_Sessions_Page**: The frontend page that lists stored historical sessions.
- **Setup_Lab_UI**: The frontend module `frontend/setup_lab.js`.
- **Setup_Link**: The "Use telemetry in Setup Experiment" workflow that sends selected imported measurements to a TRIALFORGE experiment.
- **Setup_Configuration**: The actual car setup (factor values) under which the telemetry was recorded.
- **Verified_Factor_Mapping**: A user-supplied mapping of a session's Setup_Configuration to TRIALFORGE factors, with a verified flag set by the user.
- **Release_Suite**: The script `scripts/check-release.sh` and every test and check it runs.

## Requirements

### Requirement 1: Unified provider architecture

**User Story:** As a developer, I want OpenF1 and CSV Upload to share one import pipeline, so that every feature built for historical data also works for uploaded data without duplicated code.

#### Acceptance Criteria

1. THE Import_System SHALL expose exactly two providers, OpenF1_Provider and CSV_Provider, through a common provider interface, so that a registry query lists exactly 2 providers and both implement every method of that interface.
2. THE Import_System SHALL pass every provider's output through the same normalised telemetry model before calling the Historical_Repository, so that the Historical_Repository receives no provider-specific record shape.
3. THE CSV_Provider SHALL store sessions through the Historical_Repository and the existing `f1_hist_*` tables, so that no CSV session data is written to a table outside the `f1_hist_*` set.
4. THE Import_System SHALL read both providers' stored sessions through the existing analysis code in `integrations/historical/analysis.py`, so that analysis of an OpenF1 session and a CSV session uses the same code path.
5. WHEN the OpenF1_Provider imports a session, THE Import_System SHALL produce stored records identical in every column value and row count to those produced by the pre-feature import of the same session, excluding the `provider`, `source_type` and `verification` fields added by criterion 7.
6. WHERE a column or table in the existing schema cannot represent a CSV field, THE Import_System SHALL add the field through a database migration in `db/migrations`, so that existing rows are preserved unchanged and the migration applies without error to a database holding pre-feature data.
7. WHEN a session is stored, THE Import_System SHALL record `provider`, `source_type` and `verification` on that session, using `openf1` / `external_provider` for OpenF1 sessions and `csv_upload` / `user_uploaded` / `unverified` for CSV sessions.
8. IF a provider's output fails conversion to the normalised telemetry model, THEN THE Import_System SHALL reject the import, return an error indicating the provider and the reason for failure, and leave the Historical_Repository with no rows from that import.
9. IF a session is stored without a value for `provider`, `source_type` or `verification`, THEN THE Import_System SHALL reject the write, return an error indicating the missing field, and leave existing stored sessions unchanged.

### Requirement 2: Lap summary CSV recognition

**User Story:** As an engineer, I want to upload a lap and sector summary file, so that I can browse lap times without needing sampled telemetry.

#### Acceptance Criteria

1. WHEN a CSV file is uploaded whose header row contains the columns `lap`, `sector_1_s`, `sector_2_s` and `sector_3_s`, THE CSV_Importer SHALL classify the file as a Lap_Summary_CSV. Column names SHALL be matched case-insensitively after trimming leading and trailing whitespace.
2. WHERE the columns `lap_time_s`, `driver` or `sector_4_s` are present in a Lap_Summary_CSV, THE CSV_Importer SHALL import their values for every data row, and SHALL store an empty cell in any of these columns as a missing value (null), not as zero or an empty string.
3. WHEN `lap_time_s` is absent from a Lap_Summary_CSV, THE CSV_Normalizer SHALL mark lap time as unavailable for every lap in the file and SHALL NOT substitute the sum of the sectors as a measured lap time.
4. IF a Lap_Summary_CSV lacks any of the four required columns (`lap`, `sector_1_s`, `sector_2_s`, `sector_3_s`), THEN THE CSV_Importer SHALL reject the file, SHALL return an error that lists every missing required column name, and SHALL store no session, lap or telemetry rows from that file.
5. IF a data row in a Lap_Summary_CSV has a `lap` value that is not a positive integer, or a sector value (`sector_1_s`, `sector_2_s`, `sector_3_s`, `sector_4_s`) or `lap_time_s` value that is non-numeric, negative or zero, THEN THE CSV_Importer SHALL reject the file, SHALL return an error identifying the row number and column name of the first invalid value, and SHALL store no rows from that file.
6. WHEN a Lap_Summary_CSV is stored, THE Historical_Repository SHALL store its laps in `f1_hist_lap` and SHALL write zero rows to `f1_hist_telemetry`.
7. THE Import_System SHALL assign the Data_Type_Label `Lap summaries` to every session stored from a Lap_Summary_CSV.
8. THE Import_System SHALL NOT represent a Lap_Summary_CSV as sampled telemetry in any API response or user interface, including by omitting any telemetry channel, sample count or telemetry chart for a session stored from a Lap_Summary_CSV.

### Requirement 3: Sampled telemetry CSV recognition

**User Story:** As an engineer, I want to upload sampled telemetry channels, so that I can analyse speed, throttle, brake and other traces like OpenF1 data.

#### Acceptance Criteria

1. WHEN a CSV contains a time column (`timestamp` or `session_time_s`) and at least one telemetry channel column, THE CSV_Importer SHALL classify the file as a Sampled_Telemetry_CSV.
2. THE CSV_Importer SHALL accept the Standard_Fields `lap_number`, `distance_m`, `speed_kph`, `throttle_pct`, `brake_pct`, `steer_pct`, `gear`, `rpm` and `drs`.
3. WHERE a channel is supported by the historical store, THE CSV_Importer SHALL accept additional channel columns through the Column_Mapping.
4. IF a Standard_Field has no mapped column, THEN THE CSV_Normalizer SHALL mark that channel unavailable for the session and SHALL NOT write any value for it.
5. IF a sample has an empty cell or a non-numeric value for a mapped channel, THEN THE CSV_Normalizer SHALL store that value as missing and SHALL NOT fill it by interpolation, a default or a copy of a neighbouring sample.
6. IF a Sampled_Telemetry_CSV has no time column, or the time column is mapped but every row has an empty or unparseable time value, THEN THE CSV_Importer SHALL reject the file, SHALL display an error identifying the missing or unusable time column, SHALL store no session data, and SHALL NOT generate timestamps.
7. WHEN a Sampled_Telemetry_CSV is stored, THE Import_System SHALL assign the Data_Type_Label `Full telemetry` to the stored session.
8. WHEN a file contains both lap summary columns and sampled telemetry columns, THE CSV_Importer SHALL require the user to choose one data type (lap summary or sampled telemetry) before import, and SHALL NOT store any data until the user has chosen.
9. IF a sample row has an empty or unparseable value in the time column while at least one other row has a valid time value, THEN THE CSV_Importer SHALL skip that row, SHALL NOT generate a timestamp for it, and SHALL report the count of skipped rows to the user after import.

### Requirement 4: Column inspection and mapping

**User Story:** As an engineer, I want to map my file's column names to standard fields, so that files from different tools can be imported.

#### Acceptance Criteria

1. WHEN a CSV is uploaded, THE CSV_Importer SHALL return the column names in file order, the total count of data rows excluding the header row, and the first 20 data rows, or all data rows if the file has fewer than 20.
2. WHEN a CSV is uploaded, THE CSV_Importer SHALL suggest a Column_Mapping by matching each column name to a Standard_Field name case-insensitively, after trimming leading and trailing whitespace, and SHALL leave a column or Standard_Field unmapped if no exact match exists.
3. WHEN the user submits a Column_Mapping, THE CSV_Importer SHALL accept the mapping only if each Standard_Field maps to at most one column and each column maps to at most one Standard_Field.
4. IF a submitted Column_Mapping maps more than one column to the same Standard_Field, or the same column to more than one Standard_Field, THEN THE CSV_Importer SHALL reject the mapping, return an error that names each duplicated Standard_Field or column, and leave any previously stored Column_Mapping unchanged.
5. IF a Column_Mapping refers to a column that does not exist in the file, THEN THE CSV_Importer SHALL reject the mapping, return an error that names every missing column, and leave any previously stored Column_Mapping unchanged.
6. WHEN a submitted Column_Mapping passes validation, THE Import_System SHALL store the confirmed Column_Mapping with the session, replacing any previously stored Column_Mapping for that session.

### Requirement 5: Units, timestamps and normalisation

**User Story:** As an engineer, I want times, speeds and distances converted correctly, so that uploaded data is comparable with OpenF1 data.

#### Acceptance Criteria

1. WHEN the user selects the time unit `seconds` or `milliseconds`, THE CSV_Normalizer SHALL convert every value in the mapped time column to seconds as a floating-point number, dividing `milliseconds` values by 1000.
2. WHEN the user selects a speed unit of `kph`, `mph` or `m/s`, THE CSV_Normalizer SHALL convert every value in the mapped speed column to `kph`, using 1 mph = 1.609344 kph and 1 m/s = 3.6 kph.
3. WHEN the user selects a distance unit of `metres`, `kilometres` or `feet`, THE CSV_Normalizer SHALL convert every value in the mapped distance column to metres, using 1 kilometre = 1000 metres and 1 foot = 0.3048 metres.
4. IF the user selects a unit outside the supported list for a field, THEN THE CSV_Importer SHALL reject the request, SHALL list the supported units for that field in the error indication, and SHALL leave previously stored data unchanged.
5. WHEN a `timestamp` column is mapped, THE CSV_Normalizer SHALL parse values in ISO 8601, `YYYY-MM-DD HH:MM:SS[.fff]`, `HH:MM:SS.fff`, epoch seconds and epoch milliseconds formats. Numeric values with an absolute value below 1e11 SHALL be treated as epoch seconds, and values of 1e11 or above SHALL be treated as epoch milliseconds.
6. WHEN a timestamp has a timezone offset or the user supplies a timezone, THE CSV_Normalizer SHALL convert the timestamp to UTC. IF both an offset and a user-supplied timezone are present, THEN THE CSV_Normalizer SHALL apply the offset in the value.
7. IF a timestamp has no offset and the user supplies no timezone, THEN THE CSV_Normalizer SHALL record the timezone as an Unavailable_Marker and SHALL use relative session time in seconds, measured from the earliest timestamp in the file as 0.
8. IF a timestamp value cannot be parsed in any supported format, THEN THE CSV_Importer SHALL report the 1-based data row number (excluding the header) and the original value in the validation result, and SHALL exclude that row from the normalised output.
9. IF a `lap_number` column is not mapped and a Lap_Summary_CSV for the same session is not supplied, THEN THE CSV_Normalizer SHALL mark lap assignment as unavailable for every sample and SHALL NOT infer laps.
10. WHERE a `lap_number` column is mapped, THE CSV_Normalizer SHALL assign each sample to the lap given in that column. IF a `lap_number` value is not an integer of 1 or greater, THEN THE CSV_Importer SHALL report the row number and value in the validation result and SHALL mark lap assignment unavailable for that row.
11. FOR ALL valid input values, converting a value to the standard unit and back to the original unit SHALL return the original value within a relative tolerance of 1e-9 (round-trip property).
12. IF a value in a mapped time, speed or distance column is non-numeric, empty, or not finite, THEN THE CSV_Importer SHALL report the row number, column and value in the validation result and SHALL exclude that value from conversion.

### Requirement 6: Row validation

**User Story:** As an engineer, I want the importer to find bad rows before saving, so that bad data does not reach the analysis views.

#### Acceptance Criteria

1. WHEN a CSV is validated, THE CSV_Importer SHALL report, for each mapped column, the count of missing values, where a missing value is an empty cell or a cell containing only whitespace.
2. IF two or more samples have the same time value and the same lap, THEN THE CSV_Importer SHALL report them as duplicates, listing the row numbers of every sample in each duplicate group.
3. IF a sample's time value is lower than the preceding sample's time value, THEN THE CSV_Importer SHALL report an out-of-order timestamp with the row number of the out-of-order sample.
4. IF a numeric field contains a non-empty value that cannot be parsed as a number, THEN THE CSV_Importer SHALL report the row number, column name and the original value.
5. IF a percentage channel (`throttle_pct`, `brake_pct`, `steer_pct`) contains a value outside its valid range of 0 to 100 inclusive (or -100 to 100 inclusive for `steer_pct`), THEN THE CSV_Importer SHALL report the row number, column name and value.
6. IF `gear` contains a non-integer value or `drs` contains a value outside its allowed set, THEN THE CSV_Importer SHALL report the row number, column name and value.
7. WHEN the user chooses the duplicate policy `drop_exact_duplicates`, THE CSV_Importer SHALL keep the first occurrence of each group of rows with identical values in all mapped columns, drop the others, and record the number of dropped rows in the coverage report.
8. WHEN the user chooses the duplicate policy `reject`, THE CSV_Importer SHALL treat each reported duplicate as a blocking error and record the number of rows affected in the coverage report.
9. WHEN the user chooses the ordering policy `sort`, THE CSV_Importer SHALL sort the samples by ascending time value within each lap and record the number of rows moved in the coverage report.
10. WHEN the user chooses the ordering policy `reject`, THE CSV_Importer SHALL treat each reported out-of-order timestamp as a blocking error and record the number of rows affected in the coverage report.
11. IF validation finds a blocking error, THEN THE CSV_Importer SHALL prevent the import from starting, leave previously stored data unchanged, and return the list of errors, capped at 200 entries, together with the total error count.
12. THE CSV_Importer SHALL return a coverage report listing, for each Standard_Field, the number of samples with values and the percentage of the total samples, rounded to 1 decimal place.

### Requirement 7: Session metadata

**User Story:** As an engineer, I want to describe the session I upload, so that it is findable and honest about what is unknown.

#### Acceptance Criteria

1. WHEN an import is confirmed, THE CSV_Importer SHALL verify that each of the following 11 Session_Metadata items has either a user-supplied value or an Unavailable_Marker: provider, year, event name, circuit, session type, driver, session date, timezone, original filename, time unit and distance unit.
2. WHEN an import is confirmed, THE CSV_Importer SHALL set provider to the exact value `User CSV` and SHALL set the original filename to the name of the uploaded file, including its extension.
3. IF a required Session_Metadata item is neither supplied nor marked unavailable, THEN THE CSV_Importer SHALL reject the import, name every missing item in the error indication, create no session, and retain the uploaded file and the user's entered values so the user can correct them without re-uploading.
4. IF a supplied Session_Metadata value is invalid, THEN THE CSV_Importer SHALL reject the import, name each invalid item and the rule it violated in the error indication, and create no session. A value is invalid in the following cases:
   - Year is not an integer from 1950 to 2100.
   - Session date is not a valid calendar date in ISO 8601 `YYYY-MM-DD` format.
   - Timezone is not a valid IANA timezone name.
   - Time unit or distance unit is not one of the units the CSV_Importer supports.
   - Any free-text item (event name, circuit, session type, driver) is empty after trimming whitespace or longer than 200 characters.
5. THE Import_System SHALL NOT fill a missing Session_Metadata item with a guessed or default value. An item that is not supplied SHALL be stored only as an Unavailable_Marker, and only when the user explicitly marked it unavailable.
6. WHEN the file contains a `driver` column and the user selects a driver, THE CSV_Importer SHALL import only the rows whose `driver` value exactly matches the selected driver and SHALL report the number of rows imported and the number of rows excluded.
7. IF the file contains a `driver` column and the selected driver matches 0 rows, THEN THE CSV_Importer SHALL reject the import, indicate that no rows match the selected driver, and create no session.
8. WHEN the Import_System stores a session, THE Import_System SHALL store all 11 Session_Metadata items, with Unavailable_Markers preserved as markers, together with the session in the same save operation. IF the save fails, THEN THE Import_System SHALL store neither the session nor any of its Session_Metadata.
9. WHEN a session is requested, THE Session_API SHALL return all 11 stored Session_Metadata items with the session, and SHALL return each Unavailable_Marker as a marker, not as an empty or default value.

### Requirement 8: Atomic storage and provenance of the file

**User Story:** As an engineer, I want a failed import to leave no trace as a session, and I want to know exactly what file a session came from.

#### Acceptance Criteria

1. WHEN the Import_System saves a CSV import, THE Historical_Repository SHALL write all records of that import (session, laps, samples, and provenance) in a single database transaction.
2. IF any step of a CSV import fails, THEN THE Historical_Repository SHALL roll back the transaction, SHALL leave zero records from that import in the database, SHALL NOT list the session in the Session_API, and SHALL set the Import_Job state to `FAILED` with an error indicating the failed step.
3. WHEN the Historical_Repository commits a CSV session, THE Import_System SHALL store with that session the original filename (maximum 255 characters), the SHA-256 file hash (64 hexadecimal characters), the import date (UTC, ISO 8601), the Column_Mapping, the units, the Session_Metadata, the provider and the verification status.
4. WHEN the Import_Job state becomes `COMPLETED`, THE Import_System SHALL make the session visible in the Session_API within 1 second.
5. WHEN the application restarts, THE Import_System SHALL return every session previously marked `COMPLETED` with record counts and provenance values (as listed in criterion 3) identical to those stored before the restart.
6. WHEN the application restarts, THE Import_System SHALL set every Import_Job found in `QUEUED`, `PROCESSING` or `SAVING` state to `FAILED` with the reason `interrupted`, and SHALL NOT list the sessions of those jobs in the Session_API.
7. IF the application restarts while an Import_Job was in `SAVING` state, THEN THE Historical_Repository SHALL leave zero uncommitted records from that job in the database.

### Requirement 9: Import jobs, progress, cancellation and retry

**User Story:** As an engineer, I want to follow, cancel and retry an import, so that large files do not block me.

#### Acceptance Criteria

1. WHEN a CSV import is started, THE Import_System SHALL create an Import_Job in state `QUEUED` and return its identifier within 2 seconds.
2. IF a CSV import is started and the Import_Job cannot be created, THEN THE Import_System SHALL return an error indicating the job was not created and SHALL create no Import_Job, session or records.
3. THE Import_System SHALL move each Import_Job only through the transitions `QUEUED` to `PROCESSING`, `PROCESSING` to `SAVING`, `SAVING` to `COMPLETED`, and from `QUEUED`, `PROCESSING` or `SAVING` to `FAILED` or `CANCELLED`, and SHALL NOT change the state of an Import_Job once it is `COMPLETED`, `FAILED` or `CANCELLED`.
4. WHILE an Import_Job is `PROCESSING` or `SAVING`, THE Import_System SHALL report progress as an integer count of processed rows (0 to total) and an integer total row count, and SHALL update the reported count at least once every 2 seconds.
5. WHEN the user cancels an Import_Job in state `QUEUED` or `PROCESSING`, THE Import_System SHALL set the state to `CANCELLED` within 5 seconds and SHALL leave no session or partial records.
6. IF the user cancels an Import_Job in state `SAVING`, THEN THE Import_System SHALL either complete the atomic write or roll it back, and SHALL report the final state as `COMPLETED` or `CANCELLED` respectively, within 30 seconds of the cancel request.
7. IF the user cancels an Import_Job in state `COMPLETED`, `FAILED` or `CANCELLED`, THEN THE Import_System SHALL reject the cancel request with an error indicating the job is already finished and SHALL leave the job state unchanged.
8. IF an Import_Job fails, THEN THE Import_System SHALL set the state to `FAILED`, record a failure reason of 1 to 500 characters in the job status, and leave no session or partial records.
9. WHEN the user retries a `FAILED` or `CANCELLED` CSV Import_Job and the stored upload is still available, THE Import_System SHALL create a new Import_Job in state `QUEUED` with the same file, Column_Mapping and Session_Metadata, return its new identifier, and leave the original job unchanged.
10. IF the stored upload of a retried job is no longer available, THEN THE Import_System SHALL reject the retry with an error telling the user to upload the file again, and SHALL create no new Import_Job.
11. THE Import_System SHALL apply the same job states, job store and status endpoint to OpenF1_Provider jobs and CSV_Provider jobs.

### Requirement 10: Duplicate session prevention and deletion

**User Story:** As an engineer, I want the system to stop me importing the same file twice, and to delete sessions I no longer need.

#### Acceptance Criteria

1. WHEN a CSV import is requested, THE Import_System SHALL compare the file hash and the Session_Metadata key (provider, event, session type, driver, year) with stored sessions before saving any session data.
2. IF a stored session has the same file hash, THEN THE Import_System SHALL reject the import, return the identifier of the existing session, and leave all stored sessions unchanged.
3. IF a stored session has the same Session_Metadata key and a different file hash, THEN THE Import_System SHALL pause the import, present the user with the options "replace existing session" and "cancel and choose a different driver or session type", and leave all stored sessions unchanged until the user chooses.
4. IF the user chooses to cancel or does not confirm replacement within 5 minutes, THEN THE Import_System SHALL discard the pending import and leave the existing session unchanged.
5. WHEN the user confirms replacement, THE Import_System SHALL replace the existing session and its dependent records with the new import in one transaction.
6. IF the replacement transaction fails, THEN THE Import_System SHALL roll back, retain the existing session and its dependent records unchanged, and show the user an error indicating that replacement failed.
7. WHEN the user deletes a session, THE Historical_Repository SHALL remove the session and all of its dependent records from every `f1_hist_*` table in one transaction.
8. IF the deletion transaction fails, THEN THE Historical_Repository SHALL roll back, retain the session and all of its dependent records, and show the user an error indicating that deletion failed.
9. IF a session is linked to a TRIALFORGE experiment through the Setup_Link, THEN THE Import_System SHALL warn the user, name the linked experiment, require explicit confirmation before deletion, and SHALL retain the experiment's measurements and their provenance after deletion.
10. IF a session has an Import_Job in `PROCESSING` or `SAVING` state, THEN THE Import_System SHALL reject deletion of that session, leave it unchanged, and show the user an error indicating that an import is in progress.

### Requirement 11: HTTP API

**User Story:** As a frontend developer, I want clear endpoints for the whole CSV workflow, so that the picker can drive it.

#### Acceptance Criteria

1. WHEN a client submits a CSV upload to the preview endpoint, THE Import_System SHALL return a response containing the column inspection (column names and count), the suggested Column_Mapping, the detected data type, and the validation report, and SHALL NOT create a session or Import_Job.
2. WHEN a client submits a start-import request containing an uploaded file reference, a Column_Mapping, units, a validation policy and Session_Metadata, THE Import_System SHALL create an Import_Job and return its identifier within 2 seconds, without waiting for the import to finish.
3. WHEN a client requests the status of an existing Import_Job, THE Import_System SHALL return the Import_Job state and progress.
4. WHEN a client requests cancellation of an Import_Job that is queued or running, THE Import_System SHALL stop the Import_Job and return its updated status.
5. WHEN a client requests a retry of an Import_Job that is failed or cancelled, THE Import_System SHALL start a new attempt and return its status.
6. IF a client requests status, cancellation or retry for an Import_Job identifier that does not exist, THEN THE Import_System SHALL return HTTP 404 and SHALL NOT change any Import_Job.
7. IF a client requests cancellation of an Import_Job that is already completed, failed or cancelled, or a retry of an Import_Job that is queued, running or completed, THEN THE Import_System SHALL return HTTP 409 and SHALL leave the Import_Job unchanged.
8. WHEN a client submits a file hash and Session_Metadata key to the session-exists endpoint, THE Import_System SHALL return a boolean indicating whether a session matches, and the matching session identifier when one exists.
9. WHEN a client requests deletion of an existing session, THE Import_System SHALL delete the session and its stored records and return a success response.
10. IF a client requests deletion of a session identifier that does not exist, THEN THE Import_System SHALL return HTTP 404 and SHALL NOT delete any other session.
11. THE Import_System SHALL define request and response schemas for CSV metadata and Column_Mapping in `f1lab/api/schemas.py`.
12. IF a request body is invalid, THEN THE Import_System SHALL return HTTP 422 listing the name of every invalid field and SHALL NOT create an Import_Job or session.
13. IF an uploaded file exceeds the configured maximum size, THEN THE Import_System SHALL return HTTP 413 and SHALL NOT store the file.
14. WHEN a client requests sessions through the existing Session_API, THE Import_System SHALL return stored records of both providers without provider-specific read endpoints.
15. THE Session_API SHALL include in each session summary the provider, source_type, verification, Data_Type_Label, Provenance_Status and Session_Metadata.

### Requirement 12: Large file handling

**User Story:** As an engineer, I want to import long sessions, so that I do not need to cut files by hand.

#### Acceptance Criteria

1. WHEN the CSV_Importer reads a file, THE CSV_Importer SHALL process the file in chunks of at most 10,000 rows so that peak memory use does not exceed the size of the normalised result plus 50 MB, regardless of the file's row count.
2. WHEN a Sampled_Telemetry_CSV of 1,000,000 rows is imported, THE Import_System SHALL complete the import without exceeding the configured memory limit and within 300 seconds.
3. WHILE an import is running, THE Import_System SHALL report progress at least once for every 5 percent of rows processed, with each report stating the percentage of rows processed.
4. IF a file exceeds the configured maximum file size, THEN THE CSV_Importer SHALL reject the file before processing any rows, return an error naming the file size limit and its configured value, and persist no data from that file.
5. IF a file exceeds the configured maximum row count, THEN THE CSV_Importer SHALL stop reading, return an error naming the row count limit and its configured value, and persist no data from that file.
6. WHILE an import is running, THE Import_System SHALL respond to Session_API requests and job status requests within 2 seconds.
7. IF an import fails because of a memory limit, a file size limit, or a row count limit, THEN THE Import_System SHALL set the job status to failed and SHALL retain all previously imported sessions unchanged.

### Requirement 13: Provenance correctness

**User Story:** As an engineer, I want every dataset to show where it came from, so that unverified data is never mistaken for verified data.

#### Acceptance Criteria

1. WHEN the Import_System creates a session from the OpenF1_Provider, THE Import_System SHALL set the session's Provenance_Status label to exactly `OPENF1 — external provider`.
2. WHEN the Import_System creates a session from the CSV_Provider for a user-uploaded file that is not a demo or generated file, THE Import_System SHALL set the session's Provenance_Status label to exactly `USER UPLOADED — UNVERIFIED`.
3. WHEN the Import_System creates a session or measurement from a demo CSV, a generated CSV, or the simulator, THE Import_System SHALL set the Provenance_Status label to exactly `SYNTHETIC DEMO — generated test data` and SHALL set `synthetic` to true.
4. WHEN an upload completes, THE Import_System SHALL leave `verification` at its unverified default value, and SHALL NOT set `verification` to any verified value.
5. IF a record's source is a demo CSV, THEN THE Import_System SHALL reject any attempt to set `synthetic` to false on that record, SHALL return an error indicating that demo data must remain synthetic, and SHALL leave the stored record unchanged.
6. WHEN the Setup Engineering CSV attachment code in `f1lab/services/lab.py` or `f1lab/adapters/telemetry.py` creates a measurement, THE Import_System SHALL set `synthetic` and `imported` on that measurement from the Provenance_Status of the source file. A file with the `SYNTHETIC DEMO — generated test data` status SHALL produce `synthetic` = true. A file with the `USER UPLOADED — UNVERIFIED` status SHALL produce `synthetic` = false and `imported` = true. THE Import_System SHALL NOT apply one fixed value to all uploads.
7. WHEN the Import_System creates a measurement from an imported session, THE Import_System SHALL store the original filename, the file hash, the Column_Mapping, and the units on that measurement, and the stored values SHALL equal those recorded for the source session.
8. IF any of the original filename, file hash, Column_Mapping, or units is unavailable when a measurement is created from an imported session, THEN THE Import_System SHALL reject the measurement, SHALL return an error naming the missing field, and SHALL NOT persist a partial measurement.
9. WHEN the Imported_Sessions_Page renders a session, THE Imported_Sessions_Page SHALL display that session's Provenance_Status badge, with 100% of listed sessions showing a badge.
10. WHEN the Historical_Analysis_View or the Setup_Lab_UI displays imported data, THE Historical_Analysis_View or THE Setup_Lab_UI SHALL display a Provenance_Status badge whose label is identical to the label shown for the same session on the Imported_Sessions_Page.

### Requirement 14: Setup Engineering CSV adapter

**User Story:** As an engineer, I want the existing experiment CSV upload to keep working and stop inventing times, so that my experiments stay trustworthy.

#### Acceptance Criteria

1. WHEN an engineer attaches imported lap and sector measurements to a queued experiment run, THE Setup_Lab_UI SHALL associate all imported lap and sector measurements with that run and display the attached lap count and sector count for that run.
2. WHEN a CSV file is submitted for import, THE Setup Engineering telemetry adapter SHALL parse sampled channels, lap summaries and sector summaries by calling the same CSV_Normalizer as the CSV_Provider, and SHALL produce identical normalized output for identical input.
3. WHEN a row in the file contains a timestamp, THE Setup Engineering telemetry adapter SHALL store the timestamp value from the file for that row without modification, shifting or rounding.
4. IF a row in the file has no timestamp, THEN THE Setup Engineering telemetry adapter SHALL mark the timestamp of that row as unavailable and SHALL NOT assign a computed timestamp, including a 90-second lap spacing.
5. THE Setup Engineering service SHALL expose session import and experiment attachment as two separate operations in `f1lab/services/lab.py`, so that a session import completes without creating an experiment attachment and an experiment attachment is created only by an explicit attach request.
6. WHEN sector times are the only data in a file, THE Setup Engineering telemetry adapter SHALL create zero speed, RPM, brake and stability measurements.
7. IF the file cannot be parsed by the CSV_Normalizer, THEN THE Setup Engineering telemetry adapter SHALL reject the file, SHALL return an error indicating the file could not be parsed, and SHALL leave existing sessions and experiment run attachments unchanged.

### Requirement 15: Use telemetry in Setup Experiment

**User Story:** As a setup engineer, I want to send selected measurements from an imported session to an experiment, so that I can analyse them with TRIALFORGE.

#### Acceptance Criteria

1. WHEN the user opens an imported session, THE Historical_Analysis_View SHALL show a "Use telemetry in Setup Experiment" action within 1 second of the session view finishing loading.
2. WHEN the user starts the Setup_Link, THE Setup_Lab_UI SHALL let the user select exactly one driver, between 1 and 100 laps of that driver in the session, between 1 and 50 available metrics, and exactly one target experiment that is either an existing experiment or a new experiment.
3. THE Setup_Lab_UI SHALL list only metrics that exist in the session and SHALL list a metric as unavailable and not selectable when the session contains no values for that metric for the selected driver and laps.
4. WHEN the user submits a Setup_Configuration, THE Setup_Lab_UI SHALL require the user to map each experiment factor to exactly one value and to state for the mapping whether it is verified or unverified, and SHALL reject the submission with an error identifying each factor that has no value or no verification statement.
5. WHEN the Setup_Configuration is known and complete for every selected lap, THE Setup Engineering service SHALL create one TRIALFORGE measurement for each combination of selected lap and selected metric, each carrying the session's Provenance_Status.
6. IF the Setup_Configuration is unknown or incomplete for any selected lap, THEN THE Setup Engineering service SHALL NOT create any TRIALFORGE measurements for the submission, SHALL leave the target experiment unchanged, and SHALL return an error listing each missing factor by name.
7. IF the Setup_Configuration is not a Verified_Factor_Mapping, THEN THE Setup Engineering service SHALL create the measurements flagged as excluded and SHALL NOT include them in optimisation training observations.
8. WHEN measurements are created, THE Setup Engineering service SHALL store the source session identifier on each measurement, so that every measurement created by the import returns that identifier when retrieved.
9. WHEN the import into TRIALFORGE succeeds, THE Setup_Lab_UI SHALL display the count of measurements created and a control that opens the target experiment's analysis view.
10. IF the import into TRIALFORGE fails after validation, THEN THE Setup Engineering service SHALL roll back all measurements created by that import, and THE Setup_Lab_UI SHALL display an error indicating the import failed and keep the user's selections and Setup_Configuration entries.

### Requirement 16: Frontend import workflow

**User Story:** As an engineer, I want to import a CSV from the same screen I use for OpenF1, so that I learn one workflow.

#### Acceptance Criteria

1. THE Historical_Picker SHALL display a provider selector containing exactly two options, "OpenF1" and "CSV Upload", with "OpenF1" selected by default.
2. WHEN OpenF1 is selected, THE Historical_Picker SHALL display the same controls and perform the same actions as it did before this feature, with no CSV import controls visible.
3. WHEN CSV Upload is selected, THE Historical_Picker SHALL present six steps in this order: file upload, column mapping, unit selection, metadata entry, validation and preview, import start.
4. WHILE the current step is not complete, THE Historical_Picker SHALL disable the control that advances to the next step.
5. WHEN the user returns to an earlier step, THE Historical_Picker SHALL retain the values already entered in all steps.
6. WHEN the validation and preview step is displayed, THE Historical_Picker SHALL show within 3 seconds a telemetry graph for each mapped channel, plotted from the uploaded file's rows, before the import start control is enabled.
7. IF validation reports at least one blocking error, THEN THE Historical_Picker SHALL disable the import start control and list each error with its row number, showing at least the first 100 errors and the total error count.
8. WHILE an Import_Job is in a non-terminal state, THE Historical_Picker SHALL display the job state, a progress value from 0% to 100%, and an enabled cancel control, and SHALL refresh these at least once every 2 seconds.
9. WHEN the user activates the cancel control, THE Historical_Picker SHALL request cancellation of the Import_Job and display the resulting job state.
10. WHEN an Import_Job reaches `COMPLETED`, THE Historical_Picker SHALL navigate to the Imported_Sessions_Page with the newly imported session open, within 2 seconds.
11. IF an Import_Job reaches `FAILED`, THEN THE Historical_Picker SHALL display a message indicating the failure reason, and SHALL display an enabled retry control only if Requirement 9 allows retry for that failure, and SHALL retain the entered upload, mapping, unit and metadata values.
12. WHEN a duplicate session is detected, THE Historical_Picker SHALL display the existing session's identifying details and offer each option defined in Requirement 10, and SHALL NOT start the import until the user selects one option.
13. IF the import request cannot reach the backend, THEN THE Historical_Picker SHALL display an error message indicating the connection failure, keep the import start control enabled for retry, and retain all entered values.

### Requirement 17: Imported Sessions page and analysis

**User Story:** As an engineer, I want to browse and analyse CSV sessions the same way as OpenF1 sessions.

#### Acceptance Criteria

1. WHEN the Imported_Sessions_Page loads, THE Imported_Sessions_Page SHALL list every stored session of both providers (OpenF1 and CSV), with a provider label, a Data_Type_Label and a Provenance_Status badge on every row.
2. IF no sessions are stored, THEN THE Imported_Sessions_Page SHALL show an empty-state message indicating that no sessions exist and SHALL show no rows.
3. IF the session list cannot be loaded, THEN THE Imported_Sessions_Page SHALL show an error message indicating that loading failed and SHALL offer a retry action.
4. WHEN a user opens a CSV session, THE Historical_Analysis_View SHALL display the telemetry traces of that session using the same code path as for an OpenF1 session.
5. WHILE a session has Data_Type_Label `Lap summaries`, THE Historical_Analysis_View SHALL show lap and sector tables and SHALL show every telemetry channel as unavailable.
6. WHEN a user opens a CSV session, THE Historical_Analysis_View SHALL provide driver comparison of 2 drivers, lap selection of 1 lap per driver, and track telemetry charts with the same behaviour as for an OpenF1 session.
7. IF a CSV session lacks the channels needed for a chart (for example `distance_m` for a distance trace), THEN THE Historical_Analysis_View SHALL show that chart as unavailable, SHALL list the names of all missing channels, and SHALL keep all other charts of the session displayed.
8. THE Import_System SHALL NOT contain a second implementation of the historical analysis code for CSV sessions.

### Requirement 18: Test and release evidence

**User Story:** As a maintainer, I want automated and browser proof that both providers work independently, so that I can release safely.

#### Acceptance Criteria

1. THE Release_Suite SHALL include at least one automated test for each of the following CSV import cases, and each test SHALL assert the expected accept or reject outcome: a valid Lap_Summary_CSV; a valid Sampled_Telemetry_CSV; timestamps in seconds; timestamps in milliseconds; timestamps in at least two alternative formats (ISO 8601 with offset and epoch); timestamps in at least two timezones (UTC and one non-UTC offset); a file with a missing channel; a file with duplicate samples; a file with out-of-order timestamps; a file with an invalid unit; a file with a non-numeric value in a numeric column; and a file at the maximum allowed file size.
2. THE Release_Suite SHALL include at least one automated test for each of the following import lifecycle cases: rollback after a failed import leaves zero rows from that import in storage; cancellation of an in-progress import leaves zero rows from that import; retry of a failed import succeeds without creating duplicate rows; a second import of the same file is rejected as a duplicate session; session deletion removes the session and its measurements; and a session imported before an application restart is present with identical row counts after the restart.
3. THE Release_Suite SHALL include an OpenF1 regression test that compares OpenF1 import results and browsing results (session list, driver list, lap list) against a stored baseline, and the test SHALL fail IF any compared value differs from the baseline.
4. THE Release_Suite SHALL include at least one automated test for each of the following on a CSV session: session browsing, driver comparison, lap selection, and track telemetry chart rendering.
5. THE Release_Suite SHALL include at least one automated test for each provenance source (OpenF1 session, user CSV session, demo CSV), and each test SHALL assert that the source label shown to the user matches the true origin of the data.
6. THE Release_Suite SHALL include automated tests for attaching imported measurements to a Setup Engineering experiment, and SHALL include a test asserting that the attach is refused, with an error indication to the user and no change to the experiment, IF the Setup_Configuration is unknown.
7. THE Release_Suite SHALL include property-based tests, each running at least 100 generated cases, for: the unit conversion round trip (converting to the target unit and back yields the original value within a relative tolerance of 1e-9); timestamp parsing (every supported format yields the same instant as the original); and the rule that a channel marked unavailable never receives a value.
8. THE Release_Suite SHALL include browser tests that complete a CSV session import in a run where OpenF1 is not used or is unreachable, and complete an OpenF1 session import in a run where no CSV import is performed, and each test SHALL pass independently of the other.
9. WHEN `scripts/check-release.sh` is run, THE Release_Suite SHALL execute all tests required by criteria 1 to 8, exit with code 0 IF zero tests fail, and exit with a non-zero code IF one or more tests fail.
10. IF any of criteria 1 to 9 is not satisfied, THEN THE feature SHALL NOT be reported as complete.
11. WHEN a browser test finishes, THE Release_Suite SHALL store, alongside the test results, at least one screenshot or recorded test output for each browser test in criterion 8.

