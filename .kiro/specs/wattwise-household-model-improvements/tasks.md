# Implementation Plan: WATTWISE Household Model Improvements

## Overview

This plan implements the P0 requirements (1 to 10) and the preservation requirement (16) in Python under `wattwise-lab/backend/wattwise/`. The design uses concrete Python, so no language choice is needed. Requirements 11 to 15 (P1) are out of this task list and are not scheduled.

Design clarifications C1 to C11 are followed:
- C1: the existing `provenance` key keeps describing the quantity kind. A new `source_label` carries the file's Provenance_Label.
- C3: per-appliance start factors are opt-in through `appliance_start_mode` (default `shared`).
- C6: strict row-level rejection applies only to separate-column and signed import modes. Legacy single-column imports keep skipping bad rows.
- C9: `price_reading` keeps the tariff's `simulation_day` context by default.
- Every new input has a default that reproduces pre-change behaviour (Requirements 16.6, 16.7).

Property-based tests use Hypothesis, with one file per property under `wattwise-lab/tests/props/` (`test_prop_<N>_<name>.py`). This differs from the design's four grouped files so that parallel tasks never write to the same file. Each test uses `@settings(max_examples=100, deadline=None)` and a comment `# Feature: wattwise-household-model-improvements, Property N: <title>`.

No task edits `trialforge`.

## Tasks

- [x] 1. Test foundations and database migration
  - [x] 1.1 Add Hypothesis and freeze the legacy implementation
    - Add `hypothesis>=6.100` to the `test` extra in `wattwise-lab/pyproject.toml`
    - Create `wattwise-lab/tests/props/__init__.py` and `wattwise-lab/tests/legacy_reference.py`, a verbatim frozen copy of the current `simulate`, `price_at`, `summarise` and tier accumulation, taken before any change to those functions
    - _Requirements: 16.1, 16.6_

  - [x] 1.2 Add migration 11 and ORM columns
    - Append `_m011_ww_provenance_and_export` to `db/migrations/versions.py`, guarded by `PRAGMA table_info` checks
    - Add `ww_meter_import.provenance` (NOT NULL, default `'unknown'`), `total_import_kwh`, `total_export_kwh`, and `ww_meter_reading.export_kwh`, `export_kw` (NOT NULL, default 0)
    - Backfill `total_import_kwh` from the sum of `energy_kwh` and set `total_export_kwh` to 0
    - Add the same columns to `db/models/wattwise.py`; do not edit migration 6
    - _Requirements: 3.8, 9.6, 9.8, 16.6_

- [x] 2. Tariff model: window context, billing cycle and interval splitting
  - [x] 2.1 Implement tariff context and pricing changes in `adapters/tariff_model.py`
    - Add `MissingStartDate`, `WindowContext` (with `for_hour`), `window_context(tariff, start_date)` and `reference_date(month, day_type)` (Tuesday or Saturday, never the last day of a month)
    - Let `price_at`-style helpers and `_summarise_extended` accept an optional `ctx`, and an optional `billing_cycle_kwh` that seeds `cum` only for tiered tariffs with `tier_basis == "billing_period"`
    - Add `Slice` and `price_reading(tariff, start_local, duration_s, kwh, cum_import_kwh, use_real_dates=False)`, splitting at tariff period boundaries and local midnight with `zoneinfo`
    - With no context and no billing value, behaviour must equal today's
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 7.2, 7.3, 7.5, 8.3, 8.6_

  - [x] 2.2 Write property test for post-midnight pricing
    - **Property 15: Post-midnight steps use their own date**
    - **Validates: Requirements 6.2, 6.3, 6.4, 6.5**

  - [x] 2.3 Write property test for slab start value
    - **Property 16: Slab accumulation starts at the supplied value**
    - **Validates: Requirements 7.2, 7.3, 7.5**

  - [x] 2.4 Write property test for slab monotonicity
    - **Property 17: Slab cost is monotonic in consumption**
    - Generate tariffs with non-decreasing tier adders (clarification C5)
    - **Validates: Requirements 7.8**

  - [x] 2.5 Write property test for reading splits
    - **Property 20: Reading splits conserve energy and cost**
    - **Validates: Requirements 8.1, 8.2, 8.3, 8.6, 8.7**

- [x] 3. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Meter import and meter library
  - [x] 4.1 Extend the importer in `adapters/meter_import.py` (and `meter_csv.py` / `meter_adapter.py` where needed)
    - Add `Reading.export_kwh` and `export_kw` (defaults 0.0), and `ImportOptions.export_column` and `signed`
    - Implement separate-column and signed modes with strict rejection naming row and column; reject export on cumulative registers; keep legacy mode lenient (C6)
    - Add a final pass that drops readings with non-positive or non-finite intervals, logs each timestamp as skipped, and keeps the others unchanged
    - Add `format_canonical(readings)` and `CANONICAL_OPTIONS`
    - _Requirements: 8.4, 9.1, 9.2, 9.3, 9.4, 9.5, 9.10, 9.11_

  - [x] 4.2 Update `services/meter_library.py` for provenance and export
    - Add `PROVENANCE_LABELS`, `ProvenanceError`, `normalize_provenance`, and a `provenance_badge(label)` function returning text and CSS class
    - Thread `provenance` through `import_csv` and `_view`; treat NULL as `unknown`; validate before any write
    - Store `export_kwh`, `export_kw` and both totals; return them from `_view` and the readings accessor
    - Keep the fixed quantity-kind `provenance` key and add `source_label` (C1)
    - _Requirements: 3.1, 3.2, 3.3, 3.8, 3.9, 3.10, 9.6, 9.8_

  - [x] 4.3 Write property test for invalid-interval readings
    - **Property 21: Invalid-interval readings are excluded without side effects**
    - **Validates: Requirements 8.4**

  - [x] 4.4 Write property test for signed column split
    - **Property 22: Signed column split**
    - **Validates: Requirements 9.2, 9.3, 9.9**

  - [x] 4.5 Write property test for separate columns and totals
    - **Property 23: Separate import and export columns are preserved and totalled**
    - **Validates: Requirements 9.1, 9.6**

  - [x] 4.6 Write property test for whole-file rejection
    - **Property 24: Invalid export-mode values reject the whole file**
    - **Validates: Requirements 9.4, 9.5**

  - [x] 4.7 Write property test for canonical round trip
    - **Property 25: Canonical format round trip**
    - **Validates: Requirements 9.10, 9.11**

  - [x] 4.8 Write property test for provenance normalisation
    - **Property 6: Provenance normalisation and validation**
    - **Validates: Requirements 3.1, 3.2, 3.9**

  - [x] 4.9 Write property test for provenance round trip
    - **Property 7: Provenance round trip**
    - Include a row created at schema version 10 that must read back as `unknown`
    - **Validates: Requirements 3.8, 3.10**

  - [x] 4.10 Write property test for badges
    - **Property 8: Exactly one badge per label**
    - **Validates: Requirements 3.4, 3.5, 3.6**

  - [x] 4.11 Write property test that baseline ignores export
    - **Property 26: Baseline ignores export**
    - **Validates: Requirements 9.7**

- [x] 5. Baseline profiles, overlap check and subtraction
  - [x] 5.1 Add day-type profiles and billing-cycle default to `services/baseline.py`
    - Add `NoCompleteDay`, `InvalidDayType`, `day_type_of_date`, `profiles`, `select_profile`, and extend `analyse()` with `profiles`, `day_counts` and a `source_label` argument; keep `profile_15min_kw` computation unchanged
    - Pass `source_label` through `calibration()` and keep the fixed quantity-kind `provenance` (C1)
    - Add `billing_cycle_default(readings, tz, day)` returning `{kwh, notice}`
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 7.6, 7.7, 3.7_

  - [x] 5.2 Add overlap detection and subtraction to `services/baseline.py`
    - Add `ModelledLoad`, `SubtractionResult`, `BaselineOverlap`, `BaselineUnavailable`, `quarter_hour_series`, `check_overlap` (prefix-sum rise detector, constants recorded) and `subtract_declared` with clipping metadata
    - _Requirements: 10.1, 10.2, 10.4, 10.5, 10.7, 10.8_

  - [x] 5.3 Write property test for day-type profiles
    - **Property 12: Day-type profiles are per-slot means**
    - **Validates: Requirements 5.1, 5.2, 5.5**

  - [x] 5.4 Write property test for profile recombination
    - **Property 13: Day-type profiles recombine to the all-days profile**
    - **Validates: Requirements 5.8**

  - [x] 5.5 Write property test for day-type selection
    - **Property 14: Day-type selection rules**
    - **Validates: Requirements 5.2, 5.3, 5.4, 5.6**

  - [x] 5.6 Write property test for billing-cycle default
    - **Property 19: Billing-cycle default**
    - **Validates: Requirements 7.6, 7.7**

  - [x] 5.7 Write property test for overlap detection
    - **Property 27: Overlap detection blocks every contained appliance**
    - **Validates: Requirements 10.2**

  - [x] 5.8 Write property test for subtraction
    - **Property 28: Subtraction is non-negative and reports clipping**
    - **Validates: Requirements 10.4, 10.5, 10.8**

- [x] 6. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Household model
  - [x] 7.1 Add the factor catalog helpers to `adapters/household_model.py`
    - Add constants `CHARGER_MIN_KW`, `CHARGER_MAX_KW`, `DEFAULT_CHARGER_MAX_KW`, `MAX_FLEXIBLE_APPLIANCES`
    - Add `appliance_id`, `appliance_key`, `factor_limits`, `baseline_setup`, `ChargerCapacityError`, `FactorConflict` and `check_setup_keys`
    - Leave `FACTORS` unchanged so `/api/meta` still passes `test_api_workflow.py`
    - _Requirements: 1.2, 1.6, 1.7, 2.2, 4.1, 4.3, 4.7, 4.8_

  - [x] 7.2 Update `simulate()` in `adapters/household_model.py`
    - Support `ev is None` (zero EV power, no `departure_charge_pct`, `ev_energy_kwh = 0`)
    - Raise `ChargerCapacityError` before any computation when `charge_power_kw` exceeds `charger_capacity_kw`
    - Resolve per-appliance starts with fallback to `appliance_start`; record `by_appliance` per step
    - Build a `WindowContext` from `window_start_date` or `reference_date` and pass it through pricing; use `billing_cycle_kwh`
    - Apply the Household_Model changes to `price_at`, `period_label` and `summarise` with an optional `ctx`
    - _Requirements: 1.5, 2.3, 2.6, 4.5, 4.6, 6.1, 6.2, 6.3, 6.6, 7.2, 7.3_

  - [x] 7.3 Write property test for charging range
    - **Property 2: Charging range follows capacity**
    - **Validates: Requirements 1.2, 1.6, 1.7**

  - [x] 7.4 Write property test for over-capacity rejection
    - **Property 3: Over-capacity charging power is rejected**
    - **Validates: Requirements 1.5**

  - [x] 7.5 Write property test for no-EV simulation
    - **Property 4: No-EV simulation has no EV load**
    - Use the base load plus all appliance energy (C2)
    - **Validates: Requirements 2.2, 2.3, 2.6**

  - [x] 7.6 Write property test for backward compatibility
    - **Property 5: Backward compatibility with the previous model**
    - Compare against `tests/legacy_reference.py` for simulation, tariff pricing, meter pricing and import
    - **Validates: Requirements 2.5, 4.6, 6.6, 7.3, 7.5, 8.5, 9.8, 16.6, 16.7**

  - [x] 7.7 Write property test for appliance independence
    - **Property 10: Appliance independence**
    - **Validates: Requirements 4.5, 4.9**

  - [x] 7.8 Write property test for legacy start expansion and conflict
    - **Property 11: Legacy start factor expansion and conflict**
    - **Validates: Requirements 4.7, 4.8**

- [x] 8. API schemas and validators
  - [x] 8.1 Update `api/schemas.py`
    - Add `validate_charger_capacity`, `validate_appliances`, `validate_billing_consumption` with the messages given in the design
    - Add `ApplianceIn.id` and `in_baseline`, `EVSettings.in_baseline`, `ev: EVSettings | None` on settings and both request models, and the no-EV-without-appliance validator
    - Add `charger_capacity_kw`, `appliance_start_mode` (default `shared`), `day_type`, `window_start_date`, `billing_cycle_consumption_kwh`, `baseline_excludes_modelled_appliances`, and `MeterImportRequest.provenance`
    - Widen `HouseholdSettings.charger_power_kw` to [1.4, 350] and raise appliance list caps to 20
    - Make `_factors_ok` use the household-specific catalog
    - _Requirements: 1.1, 1.3, 1.4, 2.4, 3.9, 4.2, 4.4, 5.6, 7.1, 7.4, 10.3_

  - [x] 8.2 Write property test for charger capacity validation
    - **Property 1: Charger capacity validation**
    - **Validates: Requirements 1.1, 1.3, 1.4**

  - [x] 8.3 Write property test for billing-cycle validation
    - **Property 18: Billing-cycle consumption validation**
    - **Validates: Requirements 7.1, 7.4**

- [x] 9. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 10. LabService and API wiring
  - [x] 10.1 Wire the household catalog and factors in `services/lab.py` and its readers
    - Switch `build_spec`, `_summary`, the adapter's `__init__`/`validate`, `day()`, `narrative.schedule_text` to the household-specific catalog
    - Skip EV factors, the `departure_charge_pct` constraint and measurement when there is no EV; fall back to `ofat` with a `notes` entry when fewer than two factors remain (confirm against the SDK)
    - Create one `appliance_start__<token>` factor per flexible appliance only when `appliance_start_mode == "per_appliance"`
    - Call `check_setup_keys` before any store write
    - Replace internal `simulate(BASELINE, hh)` calls with `baseline_setup(hh)` in `lab.py`, `comparison.py` and `narrative.py`; make EV rows optional in `comparison.build` and `narrative.recommendation`
    - Treat out-of-range stored charger capacity as unset with a notice in `HouseholdStore.get`
    - _Requirements: 1.2, 1.6, 2.1, 2.2, 2.5, 4.1, 4.3, 4.7, 4.8, 16.6, 16.7_

  - [x] 10.2 Wire baseline, day type, window date, billing cycle and overlap check in `services/lab.py`
    - Pass `day_type` unchanged to `select_profile` and store `baseline_source`
    - Copy `window_start_date` and `billing_cycle_kwh` into the household dict
    - Implement the study flow from the design: `not_applicable`, `BaselineUnavailable`, `skipped`, `subtracted`, `blocked`, `passed`; store `baseline_check` in spec metadata and return it from `detail()`
    - Add `billing_cycle_default` lookup that catches read errors and returns a notice
    - _Requirements: 5.7, 6.1, 7.1, 7.6, 7.7, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 3.7_

  - [x] 10.3 Make meter pricing interval-aware in `services/lab.py`
    - Rewrite `_meter_summary` to price each `Reading` with its own `interval_minutes` through `price_reading`, aggregating into the existing output keys
    - Stop using the median interval to price anything; keep it only for the 60-minute limit and series sampling
    - Convert simple CSV readings to `Reading` objects in `_meter_read`
    - Set `source_label` on result provenance without changing the quantity-kind `provenance` (C1)
    - _Requirements: 8.1, 8.2, 8.3, 8.5, 8.6, 3.7_

  - [x] 10.4 Update `api/app.py`
    - Map `BaselineOverlap`, `BaselineUnavailable`, `ProvenanceError`, `FactorConflict`, `ChargerCapacityError`, `MissingStartDate`, `NoCompleteDay`, `InvalidDayType` to HTTP 422
    - Add `GET /api/meter/imports/{id}/billing-cycle-default?day=YYYY-MM-DD`
    - Expose `provenance_badges` in `/api/meta`; keep the `FACTORS` key set unchanged
    - _Requirements: 3.4, 3.5, 3.6, 3.9, 7.6, 7.7, 16.6_

  - [x] 10.5 Write property test for appliance start factor generation
    - **Property 9: Appliance start factor generation**
    - **Validates: Requirements 4.1, 4.2, 4.3, 4.4**

  - [x] 10.6 Write example and integration tests
    - No-EV end-to-end study with one flexible appliance, and rejection of no EV with no appliances leaving stored settings unchanged
    - Rejected charger capacity leaves stored settings unchanged; default range 1.4 to 11 kW
    - Migration test from a version-10 database; demo import labelled `synthetic_demo`
    - Each overlap check status, empty baseline block, and a timed run with 35,040 quarter-hours and 20 appliances under 5 seconds
    - Friday and month-end window start dates; `window_context(..., None)` raises
    - _Requirements: 1.3, 1.4, 2.1, 2.4, 3.3, 3.8, 6.4, 6.5, 6.7, 10.1, 10.3, 10.6, 10.7_

- [x] 11. Frontend
  - [x] 11.1 Update `frontend/usage.js` and `frontend/style.css`
    - Add the provenance `<select id="u-prov">` to the import form and send `provenance`; demo buttons send `synthetic_demo`
    - Render exactly one badge from `META.provenance_badges` on the Detail_Screen and import preview; add `.prov-u`
    - Replace the `MEASURED` text with the label's badge text when `source_label` is not `real_meter`
    - _Requirements: 3.3, 3.4, 3.5, 3.6_

  - [x] 11.2 Update the study form in `frontend/app.js`
    - Add charger capacity, "I have no EV" toggle, appliance id column, day type, window start date, billing cycle field with default and notice, baseline-excludes confirmation, and appliance start mode select
    - _Requirements: 1.1, 2.1, 4.1, 5.7, 7.6, 7.7, 10.3_

  - [x] 11.3 Write Playwright test for provenance badges
    - Extend `test_ui_browser.py` setup to check the three labels show the correct single badge
    - _Requirements: 3.4, 3.5, 3.6_

- [x] 12. Preservation and layer separation
  - [x] 12.1 Write layer-separation smoke test
    - Assert no `trialforge` file imports `wattwise`, and no electricity terms appear in identifiers added to `trialforge`
    - _Requirements: 16.3, 16.4, 16.5_

- [-] 13. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise. All 133 previously passing tests must pass unmodified (Requirements 16.1, 16.2); `test_application_runs_on_the_installed_sdk_not_a_copy` stays out of scope.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- P1 requirements 11 to 15 are not scheduled; the design's extension points keep them possible later
- Each task references specific requirements for traceability
- Property tests live one per file under `wattwise-lab/tests/props/`
- Clarifications C1 to C11 in the design are binding; confirm C7 and the `ofat` fallback in 10.1 early

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2", "2.1", "4.1", "5.1"] },
    { "id": 1, "tasks": ["4.2", "5.2", "7.1", "2.2", "2.3", "2.4", "2.5", "4.3", "4.4", "4.7", "4.11", "5.3", "5.4", "5.5", "5.6"] },
    { "id": 2, "tasks": ["7.2", "8.1", "4.5", "4.6", "4.8", "4.9", "4.10", "5.7", "5.8", "7.3"] },
    { "id": 3, "tasks": ["10.1", "8.2", "8.3", "7.4", "7.5", "7.7", "7.8"] },
    { "id": 4, "tasks": ["10.2", "10.5"] },
    { "id": 5, "tasks": ["10.3", "10.4"] },
    { "id": 6, "tasks": ["11.1", "11.2", "10.6", "7.6"] },
    { "id": 7, "tasks": ["11.3", "12.1"] }
  ]
}
```
