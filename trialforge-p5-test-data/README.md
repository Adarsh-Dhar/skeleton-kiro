# TRIALFORGE / WATTWISE P-5 test fixtures

These CSVs were generated for the P-5 preparation step in the 26 November 2026 manual test guide.
They are **synthetic test data**, not actual household meter readings. When the importer asks for a source/provenance label, choose Synthetic or Demo.

## Files

| File | Purpose | Expected diagnostic |
|---|---|---|
| A1_demo_8days.csv | 8 days of 15-minute interval power in kW (768 readings) | Preview/import should show timestamps, interval energy/usage, peak, coverage and hourly profile. |
| A2_cumulative_register_reset.csv | Cumulative register with a drop/reset between 1001.2 and 12.0 kWh | Difference readings; identify and skip/log the reset interval; never create negative consumption. |
| A3_dst_fall_back.csv | America/New_York 2026-11-01 local interval-energy values with repeated 01:00 and 01:30 | Resolve ambiguous repeated wall-clock times in file order; flag them rather than treating them as ordinary duplicate records. Reading kind: energy per interval, kWh. Timestamp format: %Y-%m-%d %H:%M. |
| A3_dst_spring_forward.csv | America/New_York 2026-03-08 with nonexistent local 02:30 | Skip and log the nonexistent local timestamp. Reading kind: energy per interval, kWh. Timestamp format: %Y-%m-%d %H:%M. |
| A4_bad_rows.csv | Duplicate, out-of-order, nonnumeric and blank values | Expected: one duplicate, one out-of-order row, two invalid rows. |
| A5a_row_limit_20001.csv | 20,001 data rows (20,002 lines including header) | Clear row-count limit error. |
| A5b_file_size_over_2mb.csv | 19,000 data rows (below 20,001) and file larger than 2 MiB | Clear file-size limit error. |

## Import settings for A3

For both A3 files set timezone to `America/New_York`, reading kind to `energy per interval`, unit to `kWh`, and timestamp format to `%Y-%m-%d %H:%M`.

## Notes

- A1, A2, A3, A4 and A5 are all test fixtures only; they are not real data.
- A1 is generated from the formula in Appendix A of the guide.
- A5b intentionally uses long but numeric decimal values so its row count remains below the row cap while its byte size exceeds the 2 MiB cap.
