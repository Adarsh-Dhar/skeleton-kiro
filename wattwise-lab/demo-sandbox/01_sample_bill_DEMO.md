# Sample electricity bill: SYNTHETIC DEMO, NOT A REAL BILL

> **DEMO DATA. DO NOT USE AS REAL-WORLD EVIDENCE.** Fictional DISCOM, consumer, tariff and amounts, made up to exercise
> WATTWISE in a sandbox database. It was not issued by any utility and must never be entered into a real installation or
> described as a genuine electricity bill.

| Field | Value |
|---|---|
| DISCOM | Demo State Power Distribution Company (fictional) |
| Consumer category | LT Domestic - DEMO ONLY |
| Consumer ID | DEMO-CONSUMER-0001 (fictional) |
| Billing period | 01 Oct 2026 to 31 Oct 2026 (31 days) |
| Units billed | **447 kWh** (meter file total 446.71 kWh, rounded) |
| Amount for the period, before arrears | **Rs 2586.94** |
| Arrears | Rs 0.00 (excluded from the current-period amount) |

## Tariff (demo)

| Charge | Rate |
|---|---|
| Energy, telescopic slabs | 0–100 kWh: Rs 3.00/kWh · 101–200 kWh: Rs 4.50/kWh · above 200 kWh: Rs 6.00/kWh |
| Fixed charge | Rs 120.00 per month |
| Fuel surcharge | Rs 0.25/kWh |
| Electricity duty | 5 % of energy + fixed + fuel |
| Demand charge | not applied |

## Charge calculation for 447 kWh

| Item | Rs |
|---|---|
| First 100 kWh × 3.00 | 300.00 |
| Next 100 kWh × 4.50 | 450.00 |
| Remaining 247 kWh × 6.00 | 1482.00 |
| **Energy charge** | **2232.00** |
| Fixed charge | 120.00 |
| Fuel surcharge 447 × 0.25 | 111.75 |
| Electricity duty 5 % × 2463.75 | 123.19 |
| **Current-period total** | **2586.94** |

**Correction to the pack this was transcribed from.** That pack printed "Units billed 376 kWh". Every charge on it (the slabs
100 + 100 + 247, the fuel surcharge Rs 111.75, the total Rs 2586.94) is computed from 447 kWh, so 376 was an error. The sandbox
run reconciles both figures: 447 kWh passes and 376 kWh fails.
