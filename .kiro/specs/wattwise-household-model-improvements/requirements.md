# Requirements Document

## Introduction

This spec covers the improvements from the WATTWISE LAB household model audit. WATTWISE LAB lives under `wattwise-lab/`, with backend code in `wattwise-lab/backend/wattwise/`. The main files are `adapters/household_model.py`, `adapters/tariff_model.py` and `services/lab.py`. The meter importer is in `adapters/meter_import.py`, `adapters/meter_csv.py` and `adapters/meter_adapter.py`. The baseline logic is in `services/baseline.py`.

Work is split into three priority levels:

- **P0 (correctness and reliability)**: Requirements 1 to 10. These are the scope of the first delivery.
- **P1 (later phase)**: Requirements 11 to 15. These are lower priority and are delivered after P0.
- **P2 (out of scope)**: Real device or meter API integrations, device control, and multi-user or cloud deployment. They are recorded only as exclusions.

Constraints that apply to every requirement:

- WATTWISE owns electricity-specific logic (household, tariff, meter, baseline, provenance). TRIALFORGE owns generic experiment machinery (design of experiments, analysis, optimisation). No electricity-specific logic moves into TRIALFORGE.
- The 133 currently passing tests (household model, tariff, meter library, API workflow, DOE, analysis, optimisation, CSV import, settings) keep passing.
- The failing test `test_application_runs_on_the_installed_sdk_not_a_copy` is an environment issue and is out of scope.

## Glossary

- **WATTWISE**: The electricity-specific layer of the lab. It contains household, tariff, meter import, baseline and provenance logic.
- **TRIALFORGE**: The generic experiment machinery used by WATTWISE (design of experiments, analysis, optimisation). It contains no electricity-specific logic.
- **Household_Model**: The WATTWISE component in `adapters/household_model.py` that simulates household consumption and cost for a 16:00-to-16:00 window from factor settings.
- **Tariff_Model**: The WATTWISE component in `adapters/tariff_model.py` that prices consumption under a tariff, including slab tariffs.
- **Lab_Service**: The WATTWISE component in `services/lab.py` that sets up and runs optimisation studies.
- **Meter_Importer**: The WATTWISE component that parses meter files into readings (`adapters/meter_import.py`, `adapters/meter_csv.py`, `adapters/meter_adapter.py`).
- **Meter_Library**: The WATTWISE component in `services/meter_library.py` that stores imported meter files and their metadata.
- **Baseline_Builder**: The WATTWISE component in `services/baseline.py` that builds quarter-hour baseline profiles from meter readings.
- **Optimiser**: The TRIALFORGE optimisation machinery invoked by the Lab_Service.
- **Detail_Screen**: The frontend screen that shows the details of a meter file.
- **Charging_Factor**: The optimisation factor for EV charging power, in kW.
- **Charger_Capacity**: The maximum charging power in kW configured for the household's charger.
- **Appliance_Start_Factor**: An optimisation factor giving the start time of one flexible appliance.
- **Flexible_Appliance**: A household appliance whose start time can be shifted by the Optimiser.
- **Provenance_Label**: A label on an imported meter file stating where its data came from. Allowed values are `real_meter`, `synthetic_demo` and `unknown`.
- **Day_Type**: Either `weekday` or `weekend`.
- **Simulated_Window**: The 16:00-to-16:00 period that the Household_Model simulates.
- **Billing_Cycle**: The period over which a slab tariff accumulates consumption.
- **Billing_Cycle_Consumption**: The kWh consumed in the Billing_Cycle before the start of the Simulated_Window.
- **Reading**: One meter record with a start timestamp, an interval duration and an energy value.
- **Import_Quantity**: Energy drawn from the grid, in kWh. Always zero or greater.
- **Export_Quantity**: Energy sent to the grid, in kWh. Always zero or greater.
- **Separately_Modelled_Appliance**: An appliance (such as the EV or a Flexible_Appliance) that the Household_Model simulates explicitly, outside the baseline.
- **Baseline_Load**: The quarter-hour consumption profile for loads that are not Separately_Modelled_Appliances.

## Requirements

### Requirement 1: Enforce charger power limit (P0)

**User Story:** As a household owner, I want the recommended charging power to respect my charger, so that the lab never recommends a power my charger cannot deliver.

#### Acceptance Criteria

1. WHEN a household configuration is submitted with a Charger_Capacity, THE Household_Model SHALL accept a numeric Charger_Capacity in kW from 1.4 to 350, inclusive, with at most 2 decimal places.
2. WHEN the Lab_Service builds the Charging_Factor range for a Charger_Capacity from 1.4 kW to 350 kW inclusive, THE Lab_Service SHALL set the upper bound of the range to the Charger_Capacity, and the lower bound to 1.4 kW.
3. IF the Charger_Capacity is below 1.4 kW, THEN THE Lab_Service SHALL reject the configuration with an error that names the Charger_Capacity, the submitted value, and the minimum supported value of 1.4 kW, and SHALL leave the previously stored household configuration unchanged.
4. IF the Charger_Capacity is above 350 kW, is not a number, or has more than 2 decimal places, THEN THE Lab_Service SHALL reject the configuration with an error that names the Charger_Capacity and the accepted range of 1.4 kW to 350 kW, and SHALL leave the previously stored household configuration unchanged.
5. IF a Charging_Factor value above the Charger_Capacity reaches the Household_Model, THEN THE Household_Model SHALL reject the value with an error stating the submitted value and the Charger_Capacity, and SHALL NOT use the value in any calculation.
6. WHEN a household configuration does not specify a Charger_Capacity, THE Lab_Service SHALL use the default Charging_Factor range of 1.4 kW to 11 kW, inclusive.
7. FOR ALL Charger_Capacity values accepted by the Lab_Service, every Charging_Factor level in the generated design SHALL be greater than or equal to 1.4 kW and less than or equal to the Charger_Capacity (invariant property).

### Requirement 2: Household optimisation without an EV (P0)

**User Story:** As a household owner without an electric vehicle, I want to optimise my appliance schedule, so that I can use the lab without entering a fictional EV.

#### Acceptance Criteria

1. WHEN a household configuration has no EV and at least one Flexible_Appliance, THE Lab_Service SHALL accept the configuration and run the optimisation to completion.
2. WHEN a household configuration has no EV, THE Lab_Service SHALL produce a study design that contains zero factors named Charging_Factor and zero EV charging factors.
3. WHEN a household configuration has no EV, THE Household_Model SHALL simulate a Simulated_Window in which the EV energy is exactly 0 kWh for every time step.
4. IF a household configuration has no EV and zero Flexible_Appliance entries, THEN THE Lab_Service SHALL reject the configuration, return an error indicating that at least one optimisable load is required, and retain the previously stored configuration unchanged.
5. WHEN a household configuration includes an EV, THE Lab_Service SHALL produce a study design and results identical to those produced before this change for the same configuration and the same random seed.
6. FOR ALL household configurations without an EV, THE Household_Model SHALL produce a total simulated energy over the Simulated_Window equal to the sum of the Baseline_Load energy and the Flexible_Appliance energy, within an absolute tolerance of 0.001 kWh (invariant property).

### Requirement 3: Provenance labels for imported meter data (P0)

**User Story:** As an analyst, I want each imported meter file to show whether it is real or synthetic, so that I do not mistake demo data for measurements.

#### Acceptance Criteria

1. WHEN the Meter_Importer successfully imports a file, THE Meter_Library SHALL store exactly one Provenance_Label for the file, and the label SHALL be one of `real_meter`, `synthetic_demo`, or `unknown`.
2. WHEN the user imports a file and supplies no Provenance_Label (the value is absent, an empty string, or only whitespace), THE Meter_Library SHALL store the label `unknown`.
3. WHEN the bundled demo CSV is loaded into the Meter_Library, THE Meter_Library SHALL label it `synthetic_demo`.
4. WHEN the Detail_Screen shows a file labelled `synthetic_demo`, THE Detail_Screen SHALL show a synthetic-demo badge and SHALL NOT show a MEASURED badge.
5. WHEN the Detail_Screen shows a file labelled `unknown`, THE Detail_Screen SHALL show an unverified-source badge and SHALL NOT show a MEASURED badge.
6. WHEN the Detail_Screen shows a file labelled `real_meter`, THE Detail_Screen SHALL show the MEASURED badge and SHALL NOT show a synthetic-demo badge or an unverified-source badge.
7. WHEN the Lab_Service attaches provenance to a result, THE Lab_Service SHALL set the result's provenance to the Provenance_Label of the source file and SHALL NOT use the fixed value `historical_measured`.
8. WHEN the Meter_Library loads a meter file stored before this change that has no Provenance_Label, THE Meter_Library SHALL assign the label `unknown` to the file and SHALL leave all other stored file data unchanged.
9. IF the user supplies a non-empty Provenance_Label that is not one of `real_meter`, `synthetic_demo`, or `unknown`, THEN THE Meter_Library SHALL reject the import, SHALL store no file or label, and SHALL show an error message that lists the three allowed values.
10. FOR ALL meter files, storing then loading the file metadata SHALL return a Provenance_Label equal to the stored label (round-trip property).

### Requirement 4: Independent flexible appliance start times (P0)

**User Story:** As a household owner, I want each flexible appliance to have its own start time, so that the optimiser can schedule them separately.

#### Acceptance Criteria

1. WHEN the household configuration lists N Flexible_Appliances, where N is an integer from 1 to 20, THE Lab_Service SHALL create exactly N Appliance_Start_Factors, one per Flexible_Appliance.
2. IF the household configuration lists more than 20 Flexible_Appliances, THEN THE Lab_Service SHALL reject the configuration with an error indicating the maximum of 20 Flexible_Appliances, and SHALL create no Appliance_Start_Factors.
3. THE Lab_Service SHALL name each Appliance_Start_Factor with a name that is unique among all Appliance_Start_Factors in the household configuration and that is derived from the identifier of its Flexible_Appliance.
4. IF two Flexible_Appliances in the household configuration have the same identifier, THEN THE Lab_Service SHALL reject the configuration with an error indicating the duplicate identifier, and SHALL create no Appliance_Start_Factors.
5. THE Household_Model SHALL apply each Appliance_Start_Factor only to its own Flexible_Appliance.
6. WHEN a household configuration lists exactly one Flexible_Appliance, THE Lab_Service SHALL produce a load profile and optimisation result identical, within an absolute tolerance of 1e-9 per value, to the result produced by the former single `appliance_start` factor for the same inputs.
7. WHEN a stored study or API request uses the former `appliance_start` factor, THE Lab_Service SHALL apply that factor's value as the start time of every Flexible_Appliance in the household configuration.
8. IF a stored study or API request contains both the former `appliance_start` factor and one or more per-appliance Appliance_Start_Factors, THEN THE Lab_Service SHALL reject the request with an error indicating the conflicting factors, and SHALL leave the stored study unchanged.
9. FOR ALL pairs of Flexible_Appliances in a household configuration, changing the start time of one appliance SHALL leave the load profile of the other appliance unchanged, with an absolute difference of at most 1e-9 per time step (independence property).

### Requirement 5: Day-type-specific baseline profiles (P0)

**User Story:** As an analyst, I want weekday and weekend baselines, so that the simulated day reflects how the household actually behaves on that kind of day.

#### Acceptance Criteria

1. WHEN the Baseline_Builder reads a Meter_Library file, THE Baseline_Builder SHALL build one profile per Day_Type (`weekday` and `weekend`) containing exactly 96 quarter-hour values, each the mean of that quarter-hour across all complete days of that Day_Type, and SHALL build one all-days profile of 96 values from all complete days.
2. WHEN the calibration requests a Day_Type that has at least 1 complete day, THE Baseline_Builder SHALL return the 96-value profile for that Day_Type and SHALL report in the result metadata the Day_Type used and the number of complete days it was built from.
3. IF the file has 0 complete days for the requested Day_Type and at least 1 complete day overall, THEN THE Baseline_Builder SHALL return the all-days profile and SHALL set result metadata to indicate that a fallback occurred, the requested Day_Type, and the number of complete days used.
4. IF the file has 0 complete days overall, THEN THE Baseline_Builder SHALL return an error indicating that no complete day is available and SHALL NOT return a profile.
5. THE Baseline_Builder SHALL classify Saturday and Sunday as `weekend` and Monday to Friday as `weekday`, using the calendar date of each day in the local time zone of the Meter_Library file.
6. IF the requested Day_Type is neither `weekday` nor `weekend`, THEN THE Baseline_Builder SHALL return an error indicating an invalid Day_Type and SHALL NOT return a profile.
7. WHEN the Lab_Service runs a study, THE Lab_Service SHALL pass the study's Day_Type unchanged to the Baseline_Builder.
8. FOR ALL files with at least 1 complete day, THE Baseline_Builder SHALL produce profiles for which, at each of the 96 quarter-hours, the mean of the weekday and weekend profile values weighted by their complete-day counts equals the all-days profile value within an absolute tolerance of 1e-9 (metamorphic property), counting only Day_Types with at least 1 complete day.

### Requirement 6: Midnight crossing in tariff context (P0)

**User Story:** As an analyst, I want costs after midnight priced under the correct date, so that tariff periods that change at month or day-type boundaries are applied correctly.

#### Acceptance Criteria

1. THE Household_Model SHALL pass the start date of the Simulated_Window to the Tariff_Model.
2. WHEN a quarter-hour in the Simulated_Window has a timestamp at or after 00:00 on a calendar date later than the start date of the Simulated_Window, THE Tariff_Model SHALL use the calendar month of that quarter-hour's own date for tariff lookup.
3. WHEN a quarter-hour in the Simulated_Window has a timestamp at or after 00:00 on a calendar date later than the start date of the Simulated_Window, THE Tariff_Model SHALL use the Day_Type of that quarter-hour's own date for tariff lookup.
4. WHEN the Simulated_Window starts on a Friday, THE Tariff_Model SHALL price every quarter-hour from 00:00 to the end of the Simulated_Window on the following Saturday as `weekend`.
5. WHEN the Simulated_Window starts on the last day of a calendar month, THE Tariff_Model SHALL price every quarter-hour from 00:00 on the first day of the following month to the end of the Simulated_Window under the tariff of the following month.
6. WHEN the Simulated_Window does not cross a calendar month boundary or a Day_Type boundary, THE Tariff_Model SHALL return costs identical to the costs returned before this change, with a difference of 0.00 for every quarter-hour and for the total.
7. IF the Household_Model does not provide a start date of the Simulated_Window to the Tariff_Model, THEN THE Tariff_Model SHALL reject the request with an error indicating the missing start date and SHALL return no cost values.

### Requirement 7: Billing-cycle context for slab tariffs (P0)

**User Story:** As an analyst, I want slab tariff tiers to start from the consumption already accumulated in the billing cycle, so that one-day optimisation uses the right marginal rate.

#### Acceptance Criteria

1. WHEN an optimisation is requested, THE Lab_Service SHALL accept an optional Billing_Cycle_Consumption as a decimal number in kWh from 0 to 1,000,000 inclusive, with at most 3 decimal places.
2. WHERE the tariff is a slab tariff, WHEN a Billing_Cycle_Consumption is supplied, THE Tariff_Model SHALL start slab accumulation at that value, so that the first kWh of the day is priced at the slab containing that value.
3. WHERE the tariff is a slab tariff, WHEN no Billing_Cycle_Consumption is supplied, THE Tariff_Model SHALL start slab accumulation at 0 kWh.
4. IF the Billing_Cycle_Consumption is negative, non-numeric, or greater than 1,000,000 kWh, THEN THE Lab_Service SHALL reject the input, return an error indicating that the value must be a number from 0 to 1,000,000 kWh, and run no optimisation.
5. WHERE the tariff is not a slab tariff, THE Tariff_Model SHALL produce the same cost for any valid Billing_Cycle_Consumption, including when none is supplied.
6. WHEN the Lab_Service has a Meter_Library file with readings dated earlier in the Billing_Cycle than the optimisation day, THE Lab_Service SHALL display the sum of those readings in kWh as the default Billing_Cycle_Consumption, and THE Lab_Service SHALL let the user replace it before running the optimisation.
7. IF the Meter_Library file has no readings dated earlier in the Billing_Cycle than the optimisation day, or the file cannot be read, THEN THE Lab_Service SHALL set the default Billing_Cycle_Consumption to 0 kWh and display a notice indicating why no default was derived.
8. FOR ALL consumption profiles on a slab tariff, and for any two valid Billing_Cycle_Consumption values A and B where A is less than or equal to B, THE Tariff_Model SHALL yield a cost at B that is greater than or equal to the cost at A (metamorphic property).

### Requirement 8: Interval-aware meter reading costs (P0)

**User Story:** As an analyst, I want meter readings priced using their actual interval length, so that irregular intervals and gaps do not distort costs.

#### Acceptance Criteria

1. WHEN the Lab_Service attaches costs to Readings, THE Lab_Service SHALL compute the cost of each Reading from that Reading's own interval duration, where interval duration is the elapsed time in seconds between the Reading's start timestamp and its end timestamp.
2. THE Lab_Service SHALL NOT use a median interval duration, or any other duration aggregated across Readings, to price a Reading.
3. WHEN a Reading's interval spans one or more tariff period boundaries, THE Tariff_Model SHALL split the Reading's energy across the tariff periods in proportion to the seconds of the interval that fall in each period, so that the sum of the split energy equals the Reading's energy within 1e-9 kWh.
4. IF a Reading has an interval duration of zero seconds or less, THEN THE Meter_Importer SHALL reject that Reading with an error that names the Reading's timestamp, exclude it from cost computation, and continue to import the remaining valid Readings unchanged.
5. WHEN all Readings in a dataset have the same interval duration and no interval spans a tariff period boundary, THE Lab_Service SHALL return costs identical to those returned before this change, to the same rounding precision as the previous output.
6. WHEN a gap exists between the end timestamp of one Reading and the start timestamp of the next, THE Lab_Service SHALL price each Reading only for its own interval and SHALL NOT assign cost to the gap.
7. FOR ALL Readings, THE Lab_Service SHALL produce split period costs whose sum equals the cost of the Reading within 1e-9 in the cost currency unit (invariant property).

### Requirement 9: Solar export ingestion (P0)

**User Story:** As a household with rooftop solar, I want imported and exported energy kept separate, so that export is not mistaken for negative consumption.

#### Acceptance Criteria

1. WHEN a meter file contains separate import and export columns, THE Meter_Importer SHALL store the Import_Quantity and the Export_Quantity as separate values for each Reading, preserving each value to at least 3 decimal places of kWh.
2. WHEN a meter file contains one signed column, THE Meter_Importer SHALL store each positive value as Import_Quantity with an Export_Quantity of zero, and SHALL store the absolute value of each negative value as Export_Quantity with an Import_Quantity of zero.
3. WHEN a meter file contains only import data, THE Meter_Importer SHALL store an Export_Quantity of zero for every Reading.
4. IF an import column or an export column contains a negative value, THEN THE Meter_Importer SHALL reject the whole file, store no Readings from it, and return an error that names the row number and the column name of the first offending value.
5. IF an import column or an export column contains a value that is not a number, THEN THE Meter_Importer SHALL reject the whole file, store no Readings from it, and return an error that names the row number and the column name of the first offending value.
6. WHEN a file is imported successfully, THE Meter_Library SHALL report total imported kWh and total exported kWh as two separate values in the file metadata, each equal to the sum of the corresponding quantities across all Readings in the file.
7. THE Baseline_Builder SHALL build the Baseline_Load from the Import_Quantity only, excluding the Export_Quantity.
8. WHEN a file has no export data, THE Meter_Library SHALL return Readings, totals and baselines identical to those returned before this change, with an exported total of zero.
9. FOR ALL signed-column files, the sum of Import_Quantity minus the sum of Export_Quantity SHALL equal the sum of the original signed values, within an absolute tolerance of 0.001 kWh (invariant property).
10. FOR ALL valid files, parsing, then writing the Readings in the Meter_Importer's canonical format, then parsing again SHALL produce the same number of Readings, in the same order, with identical timestamps and with Import_Quantity and Export_Quantity values each within 0.001 kWh of the first parse (round-trip property).
11. THE Meter_Importer SHALL include a pretty printer that writes each Reading, with its Import_Quantity and Export_Quantity, in the canonical format, using at least 3 decimal places of kWh for each quantity.

### Requirement 10: Baseline double-counting validation (P0)

**User Story:** As an analyst, I want a warning when the baseline already contains a modelled appliance, so that the same load is not counted twice.

#### Acceptance Criteria

1. WHEN the Lab_Service prepares a study, THE Lab_Service SHALL compare the Baseline_Load with each Separately_Modelled_Appliance for overlap within 5 seconds for a Baseline_Load of up to 35,040 quarter-hours (one year) and up to 20 Separately_Modelled_Appliances.
2. IF the Baseline_Load contains a Separately_Modelled_Appliance, THEN THE Lab_Service SHALL block the study, return an error that names every overlapping appliance, and leave the Baseline_Load and household configuration unchanged.
3. WHERE the user confirms that the baseline excludes the Separately_Modelled_Appliances, THE Lab_Service SHALL skip the overlap check and record in the study results that the check was skipped by user confirmation.
4. WHERE the user declares in the household configuration that a Separately_Modelled_Appliance is included in the meter data, THE Baseline_Builder SHALL subtract that appliance's modelled load from the Baseline_Load at each matching quarter-hour before the study runs.
5. IF subtracting a Separately_Modelled_Appliance would make any Baseline_Load quarter-hour negative, THEN THE Baseline_Builder SHALL set that quarter-hour to zero and report the count and timestamps of the clipped quarter-hours in the result metadata.
6. WHEN the overlap check completes or is skipped, THE Lab_Service SHALL include in the study results the check status (passed, blocked, skipped, or subtracted), the names of the appliances checked, and the names of any appliances subtracted.
7. IF the overlap check cannot complete because the Baseline_Load is missing or has no quarter-hour data, THEN THE Lab_Service SHALL block the study and return an error indicating that the baseline is unavailable.
8. FOR ALL Baseline_Loads, the Baseline_Load after subtraction SHALL be zero or greater at every quarter-hour (invariant property).

### Requirement 11: Appliance operating windows (P1, later phase)

**User Story:** As a household owner, I want to set when each appliance may run, so that recommendations respect my routine.

#### Acceptance Criteria

1. THE Household_Model SHALL accept, for each Flexible_Appliance, an earliest start time, a latest start time, and a required completion time, each expressed as a time of day on a 15-minute (quarter-hour) boundary from 00:00 to 23:45.
2. THE Household_Model SHALL accept a priority for each Flexible_Appliance as an integer from 1 (lowest) to 10 (highest).
3. THE Household_Model SHALL accept a power curve per Flexible_Appliance as a sequence of 1 to 96 kW values, one per quarter-hour, where each value is from 0 to 100 kW.
4. WHEN the Lab_Service builds an Appliance_Start_Factor, THE Lab_Service SHALL limit its range to start times that are not earlier than the earliest start time, not later than the latest start time, and for which start time plus power curve duration is not later than the required completion time.
5. IF no start time satisfies an appliance's window, THEN THE Lab_Service SHALL reject the configuration, SHALL return an error that names the appliance, and SHALL leave the previously stored configuration unchanged.
6. IF an earliest start time is later than its latest start time, a priority is outside 1 to 10, or a power curve is empty, contains a value outside 0 to 100 kW, or has more than 96 values, THEN THE Household_Model SHALL reject the input, SHALL return an error that names the appliance and the invalid field, and SHALL leave the previously stored configuration unchanged.
7. WHEN two Flexible_Appliances compete for a shared limit, THE Household_Model SHALL give precedence to the appliance with the higher priority.
8. WHEN two Flexible_Appliances with equal priority compete for a shared limit, THE Household_Model SHALL give precedence to the appliance with the earlier latest start time, and if those are equal, to the appliance that was configured first.

### Requirement 12: Improved EV model (P1, later phase)

**User Story:** As an EV owner, I want a realistic charging model, so that costs and times match my vehicle and charger.

#### Acceptance Criteria

1. THE Household_Model SHALL accept, for each EV, a charging curve consisting of 2 to 101 points, where each point pairs a battery state of charge from 0 to 100 percent with a maximum charging power from 0 to 350 kW, and state of charge values are strictly increasing.
2. THE Household_Model SHALL accept a charging type of `AC` or `DC` for each EV.
3. THE Household_Model SHALL accept from 1 to 10 EVs in the household configuration.
4. WHEN the household has multiple EVs, THE Lab_Service SHALL create one charging factor per EV, and a change to the charging curve, charging type, or state of charge of one EV SHALL NOT change the charging power, energy, or cost computed for any other EV.
5. WHILE the battery state of charge of an EV rises during charging, THE Household_Model SHALL limit that EV's charging power at each simulation time step to no more than the charging curve value at that EV's state of charge, using linear interpolation between curve points.
6. WHEN a household configuration has one EV and no charging curve, THE Household_Model SHALL return results identical to those produced before this change, for the same inputs.
7. IF an EV's charging curve has fewer than 2 points, has a state of charge or power outside the allowed range, or has state of charge values that are not strictly increasing, THEN THE Household_Model SHALL reject the configuration with an error identifying the affected EV and the invalid curve value, and SHALL leave the previously stored configuration unchanged.
8. IF the charging type of an EV is missing or is not `AC` or `DC`, or the household configuration contains more than 10 EVs, THEN THE Household_Model SHALL reject the configuration with an error identifying the invalid field, and SHALL leave the previously stored configuration unchanged.

### Requirement 13: Solar generation and home battery (P1, later phase)

**User Story:** As a household with solar and a battery, I want these modelled, so that optimisation accounts for self-consumption and storage.

#### Acceptance Criteria

1. WHERE the household has solar generation, THE Household_Model SHALL, for each quarter-hour, set grid import to the greater of zero and (consumption in kWh minus solar generation in kWh), and record the amount by which solar generation exceeds consumption, when greater than zero, as Export_Quantity in kWh.
2. WHERE the household has a home battery, THE Household_Model SHALL accept capacity in kWh (greater than 0 and at most 100), maximum charge power in kW (greater than 0 and at most 50), maximum discharge power in kW (greater than 0 and at most 50), round-trip efficiency as a fraction (greater than 0 and at most 1) and initial state of charge in kWh (at least 0 and at most capacity, default 0).
3. IF a home battery input is missing, non-numeric or outside the range defined in criterion 2, THEN THE Household_Model SHALL reject the battery configuration with an error indicating which input is invalid and SHALL leave the previously stored household configuration unchanged.
4. WHERE the household has a home battery, THE Household_Model SHALL keep the battery state of charge greater than or equal to 0 kWh and less than or equal to the battery capacity at every quarter-hour, and SHALL limit energy charged or discharged in each quarter-hour to the maximum charge or discharge power in kW multiplied by 0.25 hours.
5. WHERE the household has a home battery, THE Lab_Service SHALL include battery charge start time, restricted to quarter-hour boundaries within the Simulated_Window, as a factor in its optimisation.
6. WHEN the household has neither solar nor a battery, THE Household_Model SHALL return results identical, to the same precision as the stored results, to those produced before this change for the same input.
7. FOR ALL Simulated_Windows with a battery, total energy discharged in kWh SHALL be less than or equal to total energy charged in kWh multiplied by the round-trip efficiency, plus the initial stored energy in kWh (invariant property).

### Requirement 14: Multi-day optimisation (P1, later phase)

**User Story:** As an analyst, I want to optimise across several days, so that schedules reflect weekly patterns and billing-cycle effects.

#### Acceptance Criteria

1. WHEN an analyst submits a study request specifying a number of consecutive Simulated_Windows between 1 and 31 inclusive, THE Lab_Service SHALL optimise all requested windows in one study.
2. IF the requested number of Simulated_Windows is not a whole number, is less than 1, or is greater than 31, THEN THE Lab_Service SHALL reject the request with an error indicating the permitted range of 1 to 31 and SHALL NOT start the study.
3. WHEN a multi-day study runs, THE Household_Model SHALL set the Billing_Cycle_Consumption at the start of each day after the first to the Billing_Cycle_Consumption at the end of the previous day, with a tolerance of 0.001 kWh.
4. WHEN a multi-day study runs, THE Baseline_Builder SHALL supply for each day the baseline profile that matches that day's Day_Type.
5. IF the Baseline_Builder cannot supply a profile for any day's Day_Type, THEN THE Lab_Service SHALL fail the study with an error identifying the day and Day_Type that has no profile, and SHALL NOT return partial results.
6. WHEN a multi-day study completes, THE Lab_Service SHALL report the cost for each day and the total cost, where the total cost equals the sum of the per-day costs, with a tolerance of 0.01 in the cost currency unit.
7. WHEN the number of days is one, THE Lab_Service SHALL return results identical to those of a single-window study run with the same inputs, with a tolerance of 0.01 in the cost currency unit and 0.001 kWh for energy values.

### Requirement 15: Multi-objective optimisation (P1, later phase)

**User Story:** As an analyst, I want to trade off cost, peak demand and carbon, so that I can pick a schedule that fits more than one goal.

#### Acceptance Criteria

1. WHEN a simulated run completes, THE Household_Model SHALL report total cost in the configured currency, peak demand in kW (the maximum demand over all time steps in the run) and carbon emissions in kg CO2 (the sum over all time steps of energy in kWh multiplied by carbon intensity in kg CO2 per kWh).
2. WHERE the user selects two or more objectives from cost, peak demand and carbon, THE Lab_Service SHALL pass all selected objectives to the Optimiser.
3. WHERE the user selects two or more objectives, THE Lab_Service SHALL return the set of non-dominated solutions. A solution is non-dominated if no other solution is less than or equal on every selected objective and strictly less on at least one. The set SHALL contain at least one solution and at most 100 solutions.
4. WHERE the user selects carbon as an objective, THE Lab_Service SHALL require a carbon-intensity series in kg CO2 per kWh with one non-negative value for each time step of the simulated horizon.
5. IF carbon is selected and no carbon-intensity series is available, or the series has fewer values than time steps or contains a negative or non-numeric value, THEN THE Lab_Service SHALL reject the request before starting optimisation, with an error that names the missing or invalid series and the reason. THE Lab_Service SHALL NOT start any simulation or modify stored results for the rejected request.
6. WHEN only cost is selected, THE Lab_Service SHALL return results identical to those returned before this change, for the same inputs and the same random seed, with a cost difference of 0.
7. THE Lab_Service SHALL keep objective definitions in WATTWISE and pass only generic objective values (numeric values with an objective identifier and a minimise direction) to TRIALFORGE.
8. IF the user selects no objective, THEN THE Lab_Service SHALL reject the request with an error stating that at least one objective is required, and SHALL NOT start optimisation.

### Requirement 16: Preservation of existing behaviour and layer separation

**User Story:** As a maintainer, I want existing behaviour and the layer split preserved, so that the improvements do not cause regressions.

#### Acceptance Criteria

1. WHEN the P0 changes are complete, THE test suite SHALL run the 133 tests that pass before this change, and all 133 SHALL pass with 0 failures, 0 errors, and 0 tests removed, skipped, or modified to make them pass.
2. IF any of the 133 previously passing tests fails after the P0 changes, THEN THE test suite SHALL report the failing test names and finish with a non-zero exit status.
3. THE WATTWISE layer SHALL contain all electricity-specific logic added by this spec, where electricity-specific logic means any code that references households, tariffs, meters, chargers, or appliances.
4. THE TRIALFORGE layer SHALL contain 0 occurrences of the terms household, tariff, meter, charger, or appliance (case-insensitive, including plural forms) in identifiers, field names, and user-facing text added by this spec.
5. THE TRIALFORGE layer SHALL contain 0 imports of, or dependencies on, the WATTWISE layer.
6. WHEN a request or stored study created before this change is loaded, THE Lab_Service SHALL process it without requiring the user to run any migration step, and SHALL return the same result as before this change for the same input.
7. IF a request or stored study created before this change lacks a field introduced by this spec, THEN THE Lab_Service SHALL apply the default value that reproduces the pre-change behaviour and SHALL NOT reject the request or study.

## Out of Scope (P2)

The following items are recorded for planning and have no requirements in this spec:

- Real device or meter API integrations.
- Device control, such as sending start or stop commands to chargers or appliances.
- Multi-user support and cloud deployment.
- Fixing `test_application_runs_on_the_installed_sdk_not_a_copy`, which is an environment issue.
