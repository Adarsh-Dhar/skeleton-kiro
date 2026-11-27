# WATTWISE input data: what to collect and where

WATTWISE uses **zero fabricated input data**. A fresh installation is empty: no example tariff, household, appliance, EV or
meter file. Every value you enter carries a source record, and anything missing stays missing (it is never replaced by a
default). Costs and schedules are **projections** computed from these inputs. Only meter readings you import are measurements.

This guide lists every input, its unit, what to read, and where to read it for an Indian household.

## Source record (attached to every input)

| Field | Meaning | Example |
|---|---|---|
| `source_type` | `official`, `manufacturer`, `research_dataset`, `meter_export`, `user_provided` or `unverified` | `official` |
| `source_name` | Who published the value, or `household owner` | `Karnataka Electricity Regulatory Commission` |
| `document` | URL or reference of the original document | `Tariff order FY2025-26, LT-2(a)` |
| `collected_on` | Date you read the value | `2026-11-20` |
| `effective_from` / `effective_to` | Dates the value applies (tariff orders, model years) | `2025-04-01` / `2026-03-31` |
| `verification` | `verified`, `user_supplied` or `unverified` | `verified` |

Rules the server enforces:

- `verified` requires an official, manufacturer or research source, a `document`, a `collected_on` date that is not in the future,
  **and the document retrieved by WATTWISE itself**. Paste the publisher's `https://` URL into *Document or URL* and press
  **Retrieve document**: WATTWISE downloads it, archives a copy and records its SHA-256 and the date (`POST /api/evidence/retrieve`).
  Only then can the source be `verified`, labelled **VERIFIED · DOCUMENT RETRIEVED**. A `verified` claim without that is rejected.
  If one was stored earlier, it shows as **CITED · NOT RETRIEVED**.
- Evidence is bound by the server from its own archive (`evidence_id`). An `evidence` block sent by a client is discarded, so it cannot be forged through the API.
- **Attach a file** (for example a PDF of your bill, `POST /api/evidence/attach`) archives and hashes it, but you supplied it, so it backs `user_supplied` only (**USER SUPPLIED · DOCUMENT ATTACHED**).
- Your own data (bill, meter export, a label you read, your own measurement) is `user_supplied`. It is never shown as independently verified.
- A value without a traceable source is accepted only as `unverified`, and it is labelled that way everywhere it appears.
- A tariff whose `effective_to` date has passed is flagged **EXPIRED** on the household page, in readiness warnings and in reports.
- Each experiment stores a snapshot of all its input sources. Later edits do not change it. **Export all inputs** (`/api/inputs/export.json`) returns every input with its source and status.

## 1. Electricity tariff (required)

The tariff must match your **state, DISCOM, consumer category and the date range** of your usage.

| WATTWISE field | Unit | What to collect |
|---|---|---|
| `name`, `utility`, `consumer_category`, `currency` | text | Tariff name, DISCOM, category as printed on the bill (for example "LT-1 Domestic"), `INR` |
| `periods[].price` | INR/kWh | Energy charge per kWh. With **time-of-day (ToD)** rates, add one period per time band with its start and end hour. Without ToD, add one period from 0 to 24. |
| `tiers[]` (slabs) | kWh, INR/kWh | Telescopic slabs (for example 0–100 kWh, 101–200 kWh). Enter the slab limits, and enter the extra price of each slab as `adder_per_kwh` over the period price. Choose `tier_basis = billing_period` when slabs count the whole month's consumption. |
| `fixed_monthly_charge` | INR/month | Fixed or demand charge per month (often per kW of sanctioned load: multiply by your sanctioned load) |
| `fixed_daily_charge` | INR/day | Only if your bill states a daily fixed charge |
| `demand_charge_per_kw` | INR/kW | Only for tariffs that bill recorded maximum demand |
| `surcharge_per_kwh` | INR/kWh | Fuel and power purchase cost adjustment (FPPCA, FAC, FCA) per kWh, from the bill or the DISCOM's monthly notification |
| `tax_pct` | % | Electricity duty or tax as a percentage of charges. If your state levies duty per kWh instead, add it to `surcharge_per_kwh`. |
| `export_credit_per_kwh` | INR/kWh | Net-metering or gross-metering credit for rooftop solar exports |
| `billing_days` | days | Days in your billing cycle (monthly or bimonthly) |

Where to get it:

1. **Your latest electricity bill**: `source_type: user_provided`, `verification: user_supplied`. Set `document` to the bill month and number.
2. **The state commission's (SERC/JERC) current tariff order** for your DISCOM: `source_type: official`, `verification: verified`. Set `document` to the order number and schedule, `collected_on` to the day you read it, and the effective dates from the order. The order is the primary source because the bill applies it.
3. **Cross-check:** the Central Electricity Authority's annual compendium "Tariff and Duty of Electricity Supply in India" (latest: March 2026). It summarises the notified state tariffs and electricity duty: <https://cea.nic.in/sub-sub-heading-view?lang=en&mappingId=505&subHeadingId=1001&subSubHeadingId=10008>. Cite the state order itself as the source, and use the compendium to check you have the right order and year.

When the tariff order is replaced, set `effective_to` on the old record. WATTWISE then flags it as expired.

## 2. Meter readings (strongly recommended: they replace the always-on load)

| What | Unit | What to collect |
|---|---|---|
| Interval readings | kW, W, kWh or Wh per interval, or a cumulative register | Your **15- or 30-minute** consumption for at least 14 complete days, as a CSV export |
| `source` / meter source | text, date | The portal or app you exported from, and the download date |

Where to get it: your DISCOM's **smart-meter consumer portal or app**, or the smart-meter data access route described in the Government's RDSS smart-meter FAQ (<https://www.ipds.gov.in/webappdocs/RDSS_Docs/FAQs_Smart_Meter_27102021.pdf>). Consumption data is personal: only import data you are entitled to, with the account holder's consent.

How to enter it:

1. In **My usage**, choose provenance `real meter export` and say where the file came from.
2. Map the columns and time zone (`Asia/Kolkata` for timestamps without an offset).
3. Preview, import, then press **Use as my household baseline**.

A meter export is labelled **MEASURED · USER SUPPLIED**. Files of unknown origin are **UNVERIFIED**. Synthetic or demo files are rejected.

## 3. Always-on load (only if you have no meter readings)

| Field | Unit | What to collect |
|---|---|---|
| `base_load_kw` | kW | Your measured constant background load: the lowest steady reading on the meter display between about 01:00 and 04:00, with large appliances off. A plug-in energy meter on each always-on device also works. |

Source: `user_provided` / `user_supplied`. WATTWISE uses this value as a **flat** load for every quarter hour, with no invented
evening or morning peaks. That understates real usage, so a meter baseline gives much better projections.

## 4. Appliances you can schedule

| Field | Unit | What to collect |
|---|---|---|
| `name` | text | The appliance |
| `power_kw` | kW | **Average** power over one cycle = energy per cycle (kWh) ÷ cycle length (h). Do not use the peak rating on the plate: a washing machine's heater rating is far above its cycle average. |
| `duration_h` | h | Length of the programme you actually use |
| `fixed_start` (usual start) | clock time | When you run it today: the baseline every saving is measured against |
| `flexible` | yes/no | Whether you could run it at another time |

Where to get it:

- **Best:** measure one cycle with a plug-in energy meter (kWh and minutes). `user_provided` / `user_supplied`.
- **Washing machines:** the BEE star label gives energy consumption per cycle, from a test under BEE Schedule 12. Divide by the programme length. Cite the label and model: `manufacturer` / `verified` if you record the label document and date. BEE programme page: <https://beeindia.gov.in/show_content.php?level=1&lid=322&ls_id=198>. The label portal is <https://www.beestarlabel.com>.
- **Geysers, air conditioners, microwave ovens:** BEE labels and manuals give rated input power and, for some, annual energy. Use the rated power only for appliances that draw it steadily while on.
- **Dishwashers and dryers** are not BEE-labelled in India. Use the manufacturer's programme table (kWh per programme) from the manual or spec sheet, or measure it.

## 5. Electric vehicle (optional; disabled until every value is entered)

| Field | Unit | What to collect | Source |
|---|---|---|---|
| `battery_kwh` | kWh | **Usable** battery capacity | Vehicle specification sheet or owner's manual for your model and model year (`manufacturer`; cite the document and model year) |
| `efficiency` | 0.5–1 | Charging efficiency, grid to battery | Manufacturer, if published. Otherwise measure it: kWh drawn from the wall (charger or meter) ÷ kWh added to the battery (state-of-charge change × usable capacity). `user_provided`. |
| `initial_soc_pct` | % | Typical charge when you plug in | The car's display or app over a few weeks (`user_provided`) |
| `desired_charge_pct` | % | Minimum charge you need at departure | Your requirement |
| `plug_in_hour` | clock time | When you usually plug in today | Your habit: the baseline every saving is measured against |
| `departure_hour` | clock time | When the car leaves | Your habit |
| Charger capacity (`charger_power_kw`) | kW | Rated AC output of your home charger | Charger nameplate or manual (for example 3.3 kW or 7.2 kW); `manufacturer` or `user_provided` |

One modelling assumption remains in the EV model: charging power tapers above 80 % state of charge. That is part of the model,
not an input. If your vehicle's charging curve is published, compare projected charge at departure against your car's real
behaviour before relying on it.

## 6. What WATTWISE can and cannot prove

| Claim | What WATTWISE checks | What remains your responsibility |
|---|---|---|
| A tariff or spec sheet is official | It retrieved the document from the URL itself, archived it, recorded its hash and date, and refused private or non-https addresses | That the numbers you typed match the document. WATTWISE does not read tariff orders or spec sheets. |
| A CSV is a real meter export | It hashes the file and flags patterns real meters practically never produce: every reading identical, over 90 % of consecutive readings identical, whole days repeated exactly, only round numbers | That the file is an unedited export. A careful forgery passes the checks, so the badge reads **METER EXPORT · DECLARED BY YOU**. |
| A value is your own | It records it as `user_supplied` with the date and notes you give | Its accuracy |

**Data audit.** `GET /api/audit`, the *Data audit* panel on the household page, or `python -m wattwise.audit [--db PATH]`
(exits 1 when anything is blocking) checks every stored input. Severities:

- **blocking:** an example or synthetic value, or a missing required input. It is never used.
- **to confirm:** your value, saved before source tracking. It is still used, but it stays marked until you tick *I have checked this value* and save.
- **warning:** unverified, cited but not retrieved, expired, or implausible meter readings.

Values saved before source tracking that are **exact copies of the old built-in examples** are not credited to you. That covers
the appliances 1.1 kW / 0.5 kW / 2.0 kW, the 0.35 kW always-on load and the 60 kWh EV. They are listed as example copies and kept
out of every calculation. Re-adopt one only if you have checked it, and you then have to name its source.

**Automatic data feeds.** WATTWISE does not fetch tariffs, meter readings or appliance or EV specifications on its own, because:

- India has no public machine-readable API for DISCOM tariffs. Tariffs are published as tariff-order PDFs by each state commission.
- Smart-meter data access is per DISCOM, needs the consumer's login and consent, and has no common public API.
- The BEE star-label portal has no documented public API.

The integration WATTWISE provides is document retrieval and archiving, which ties each value to the document it came from. A
per-DISCOM integration (for example parsing one commission's tariff order, or a consented meter-data download from one utility's
portal) can be added once the specific utility and its access method are known. It would be built and tested against that
utility's real documents, not guessed.

## 7. What WATTWISE never invents any more

- No seeded tariffs. The three example tariffs earlier versions installed are recognised in an existing database, labelled **EXAMPLE · NOT REAL**, and blocked from every calculation. Replace them with your own tariff and delete them.
- No default household, EV, appliances, charger power or always-on load.
- No synthetic background profile and no random noise. Every projection is deterministic and computed only from your inputs.
- No demo meter files and no demo experiment.
- No automatic fallback when an input is missing. The household page lists what is missing, and projections are refused until it is entered.
- Experiments created before this policy are marked **EXAMPLE INPUTS**, cannot be run again, and show no saving.

The values in `tests/inputs.py` and `tests/fixtures/` are **test fixtures for the automated tests only**. They are not shipped
with or seeded into the application.
