# WATTWISE demo sandbox run: SYNTHETIC DATA, NOT REAL

Database: `/Users/adarsh/Documents/skeleton-kiro-main/wattwise-lab/demo-sandbox/sandbox.db` (not the real installation).

| Step | Result |
|---|---|
| Tariff from the demo bill | `demo-lt-domestic-fictional`, UNVERIFIED |
| Meter import | 1488 rows, 446.71 kWh, 2026-09-30T18:30 to 2026-10-31T18:00 UTC, UNVERIFIED; measured 00:00-03:00 load 0.4 kW |
| Household | ready: True |
| Reconcile 447 kWh / Rs 2586.94 | **PASSED**: priced 446.71 kWh, Rs 2585.03 (-0.06% kWh, -0.07% amount) |
| Reconcile 376 kWh (pack misprint) | **FAILED**: metered 446.71 kWh vs 376 kWh on the bill (+18.81%) |
| Experiment | 5 runs, baseline check subtracted; Cheapest feasible schedule: 53.24 per day (0% below baseline) |
| Audit sign-off | **NOT PASSED** (expected: the data is fictional) |

## Sign-off criteria

- [x] no blocking findings (no example, synthetic or missing input)
- [x] no unconfirmed values saved before source tracking
- [ ] the selected tariff is from your bill (user supplied) or a retrieved official document, and not expired
- [ ] no input is unverified or cites a document WATTWISE never retrieved
- [ ] at least one meter export declared real, with no plausibility warning
- [ ] a billing period reconciles with the real bill (selected tariff, that meter export)
- [x] no experiment on example inputs remains in use
- [x] an experiment created from the confirmed inputs

## Audit findings

- warning: tariff[demo-lt-domestic-fictional] - unverified: no traceable source
- warning: appliances[Dishwasher (DEMO DW-1200-SAMPLE)] - unverified: no traceable source
- warning: meter_import[mi-a776fa5eb5] - unverified: no traceable source
