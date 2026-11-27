# WATTWISE LAB

A browser-based **experiment workstation for household electricity**, built entirely on the **TRIALFORGE** SDK
(`trialforge-core`, in [`../skeleton`](../skeleton)). Describe your tariff, your electric car and the appliances you can schedule; choose which
parts of the evening schedule to vary; project every combination from your own inputs; see which choices move the bill; let a Bayesian
optimiser look for the cheapest schedule that **still leaves the car charged by the time it leaves**. Starting it takes one command (`scripts/start-wattwise.sh`); after that everything is done in the browser and no Python or command-line knowledge is needed to *use* it.

> **ZERO FABRICATED INPUT DATA.** A fresh installation is empty: no example tariff, household, appliance, EV or meter file. Every input is
> yours (bill, meter export, appliance labels, vehicle documents) or comes from a cited real-world source, and carries a source record
> (type, name, document, collection and effective dates, verification). Missing data stays missing: projections are refused until it is
> entered. Schedule costs are **projections** from those inputs, never measurements. `verified` needs the publisher's document
> retrieved and archived by WATTWISE itself, and everything else is labelled as your own claim. `GET /api/audit` (or
> `python -m wattwise.audit`) audits a database. What to collect and where, and what WATTWISE can and cannot prove:
> [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md).

## Run it

Requires Python 3.11+. The simplest way is the start script at the repository root (`scripts/start-wattwise.sh`, see the root README):
it builds the SDK wheel if it is missing, creates `wattwise-lab/.venv`, installs everything and starts the server. By hand:

```bash
cd wattwise-lab
python -m venv .venv && . .venv/bin/activate
pip install ../skeleton/dist/trialforge_core-0.2.1-py3-none-any.whl   # build: python -m build --wheel ../skeleton/packages/trialforge-core
pip install -e ..                  # the root database package (trialforge-db)
pip install -e .
wattwise-lab                       # or: python -m wattwise
```

Open <http://127.0.0.1:8002>. **All state lives in the repository's single root database** `data/trialforge.db` (`TRIALFORGE_DB` to change), shared
with F1 SETUP LAB but scoped per app: experiments, household settings (`ww_household`), the tariff library (`ww_tariff`) and the historical
meter library (`ww_meter_import`, `ww_meter_reading`). Nothing is kept in the browser any more: household settings saved by an older version
in browser storage are uploaded once and then removed. Restart the server and everything is still there. Interactive API docs: <http://127.0.0.1:8002/api/docs>.
It runs independently of F1 SETUP LAB (port 8001); both can be installed in the same or separate environments.

## The workflow (all in the browser)

| Screen | What you do |
|---|---|
| **Energy dashboard** | See a typical day against your tariff (load stacked over price bands), plus every experiment, its progress and the saving found |
| **Household setup** | Edit tariffs (periods that wrap midnight, demand charge; gaps and overlaps are explained as you type), the EV (battery, charge needed and departure time) and appliances |
| **Experiment designer** | Pick the variables (EV charging start, charger power, flexible-appliance start), their ranges, the design method (full / fractional factorial, one-factor-at-a-time, central composite, Box-Behnken, random) and a run budget |
| **Schedule simulator** | Move three sliders and watch the day re-priced against today's schedule: cost, kWh, peak kW, charge at departure, cost per price period |
| **Run monitor** (inside an experiment) | Run the design, watch progress, cancel, retry failed runs; open any run's day; attach a smart-meter CSV to a run: plain `timestamp,power_kw`, or any format the My usage importer reads (*Other file formats*: columns, time zone, interval energy, cumulative register), priced in local time |
| **Analysis** | Cost of every schedule, cost against peak demand, factor effects with 95% intervals, interactions, Type II ANOVA, and which schedules failed the departure-charge constraint |
| **Optimisation** | Compare the best schedule with today's day, ask for suggestions (Bayesian, Optuna, random, grid), run them, follow the history |
| **Reports** | Printable HTML report, JSON, CSV exports of schedules, measurements and effects |
| **My usage** | Import your real meter export into the historical library, see exactly how each row was read, get your measured baseline, use it as the household's background load, and compare your actual usage with the optimised schedule |
| **Meter preview** | Quick preview of a `timestamp,power_kw` (or `power_w`) CSV, priced with a chosen tariff; quality problems are listed, not hidden |

No demo files are bundled. Use your own meter export (the automated tests use their own fixtures in `tests/fixtures/`, which are not
shipped). To see retries and failures, set *Injected fault rate* to 1 and *Retries* to 0 in the designer.

## Your real data

**Household settings** are stored on the server (`GET/PUT /api/household`): always-on load, appliances (average power, cycle length, usual
start, movable or not), EV (usable battery, charging efficiency, typical and required charge, usual plug-in and departure time), charger
capacity, selected tariff and the meter import used as baseline, each with its source. `GET /api/household` also returns `readiness` (what is
still missing) and `current_schedule` (today's schedule, derived from your plug-in time, charger capacity and usual appliance starts).
`POST /api/household/reset` clears everything and keeps a backup; `POST /api/household/restore` brings it back. `GET /api/inputs/export.json`
exports every input with its source and status.

**Historical meter library** (`POST /api/meter/import/preview`, `POST /api/meter/import`, `GET /api/meter/imports[/{id}[/readings|/summary|/calibration]]`,
`DELETE /api/meter/imports/{id}`). Imports are your measured usage and are kept separate from experiment-run measurements. The interpreter
(`backend/wattwise/adapters/meter_import.py`) supports:

* configurable timestamp and value columns, `,` `;` or tab delimiters, decimal commas, `strptime` formats for non-ISO timestamps;
* ISO-8601 timestamps with offsets (converted to UTC) and naive local timestamps in a chosen IANA time zone; DST fall-back hours are resolved
  by file order, spring-forward gaps are skipped and logged;
* average power (kW/W), energy per interval (kWh/Wh) or a **cumulative register** (consecutive differences). A register that goes down is a
  reset or meter change: that interval is skipped and reported, never counted as negative consumption; a configured `rollover_kwh` is handled;
* duplicate and out-of-order detection, gap reporting (gaps are never filled), optional resampling with partial buckets flagged;
* the 20,000-row / 2 MB limits of the original importer.

The preview shows the source columns, how they were interpreted, the converted readings next to the original timestamps, and every skipped or
transformed row with its line number. Your supplier's exact export format is a manual gate: if the auto-detection does not fit, set the
columns and units explicitly, or send a header plus 10–20 anonymised rows to add a preset.

**Baseline analytics** (`/summary`): daily energy (mean, spread, min/max), hourly profile, weekday vs weekend profiles, peak demand (of the
interval averages), coverage, and the cost of your readings under the selected tariff. Only days covered ≥ 95 % are used for profiles and
daily averages; with fewer than 7 complete days a warning says the baseline is unreliable. **Use as my household baseline** makes new
experiments start from the measured 15-minute profile as background load instead of the always-on load you entered (EV and flexible appliances are still
projected on top, so import a period without them or remove them from the appliance list; the app warns when the profile looks like it
includes EV charging).

**Tariffs.** Every field below can be edited in *Household setup → Electricity tariff*: each period's days (all / weekday / weekend), individual weekdays (Mon-Sun tick boxes; none ticked = every day)
and months, and an *Advanced charges* section for fixed daily and monthly charges, billing-period length, demand-charge basis, surcharge,
tax, export credit, tiered (slab) prices and the day the one-day simulator uses. Saving from the editor keeps every field; earlier versions
dropped the advanced ones. The original fields are unchanged and existing tariffs give identical costs (regression-tested). Optional, generic extensions:
weekday/weekend, individual-weekday and seasonal periods, fixed daily and monthly charges, tiered energy pricing (daily or billing-period basis), per-kWh surcharges,
tax percentage, export credits, and a billing-period demand charge. No tariff is bundled: enter your own from your bill or your DISCOM's
tariff order, with its source, and check the computed cost of a billed period against the bill (manual gate). The three example tariffs
earlier versions seeded are recognised in an existing database, labelled EXAMPLE · NOT REAL and refused by every calculation.

**My actual electricity usage vs optimised schedule** (`GET /api/comparison?import_id=&exp_id=`, printable `/api/comparison/report.html`):
historical daily usage and cost, cost by tariff period, peak, the current and best projected schedules (cost, energy, peak, EV charge at
departure), measured schedules, estimated savings and the assumptions behind them.

## Data provenance

| Label | Meaning |
|---|---|
| `historical_measured` | your imported meter readings |
| `historical_measured_priced` | those readings priced with the tariff you entered (only as accurate as that tariff) |
| `owner_entered_base_load` | the constant always-on load you entered (no meter baseline) |
| `projected` | a model estimate for a schedule |
| `measured_schedule` | readings imported to a run after the household actually used that schedule |
Where the labels appear: the *My usage* library marks each imported **file** with a source badge (MEASURED · USER SUPPLIED for a meter
export, UNVERIFIED SOURCE for a file of unknown origin; synthetic files are rejected). The data labels above are applied to figures in the baseline, comparison, experiment and report views:
`historical_measured_priced` on readings priced with your tariff, `projected` on model estimates and `measured_schedule` on readings attached to a run.
## Time convention

Schedule times are hours on a 24 h window that **starts at 16:00**: 19 is 19:00 the same evening, 31.5 is 07:30 the next morning. The screens
show clock times; the API uses the numbers.

## How it uses the SDK (and what it does *not* contain)

The application contains **only** electricity knowledge: the household model, tariff library, the simulator adapter, meter-CSV parsing and
the screens. All experiment machinery comes from `trialforge`:

| App feature | TRIALFORGE API |
|---|---|
| Experiment definition, departure-charge constraint | `ExperimentSpec`, `Factor`, `Objective`, `Constraint`, `MeasurementDefinition`, `ExecutionPolicy` |
| All design methods | `ExperimentService.design` |
| Simulator | an app-owned adapter (`wattwise/adapters/meter_adapter.py`) registered in `AdapterRegistry` |
| Background execution, retries, cancel, restart recovery | `LocalBackgroundRunner`, `ExperimentService.execute / cancel / retry_run / recover` |
| Meter import, unit conversion, duplicate / disorder flags | `MeasurementEngine`, `ExperimentService.ingest_measurements` |
| Effects, ANOVA, interactions, feasibility-aware best run | `ExperimentService.analyse` |
| Optimisation | `ExperimentService.optimise / add_runs` |
| Persistence, history, reports, CSV exports | `SQLiteExperimentRepository` (on the root database through the app-scoped subclass in `../db/scoped.py`), `ExperimentService.report`, `ReportEngine` |

Code map: `backend/wattwise/adapters` (household model, adapter, meter CSV), `services` (`LabService`, tariffs, narrative, HTML report),
`api` (FastAPI routes and schemas), `frontend/` (no-build single-page app with its own SVG charts). The app imports `trialforge` only through
its public top-level API; `tests/test_persistence_and_sdk.py` enforces this, and also that the SDK source contains no electricity or F1 terms.

## Tests

```bash
pip install -e ".[test]" && playwright install chromium
pytest                      # API, model, tariffs, meter import, persistence, SDK independence, browser workflow
```

The browser test walks the whole workflow with the mouse only and writes screenshots of every screen to `docs/screenshots/`.

## Reading the results honestly

* The recommended schedule is always the cheapest **measured, feasible** run. A schedule that leaves the car below the required charge is never
  recommended, even when cheaper, and the analysis states what the requirement costs.
* Effects are the change in *daily cost* from the low to the high end of each tested range (negative is cheaper).
* Costs are for one projected day on the tariff you entered; real savings depend on real usage and your supplier's billing rules.
* A **projected** saving is a model estimate. Only a **measured schedule** (readings imported after the household actually ran it) is evidence
  of a real saving. An experiment without a meter baseline never produces a saving against your real usage. Experiments created before
  the data policy were built on example inputs: they are marked EXAMPLE INPUTS, cannot be run again and show no saving.

## Limits and optional pieces
- **Appliances.** The household screen allows up to 6 appliances in total; the API accepts up to 20 flexible appliances
  (`MAX_FLEXIBLE_APPLIANCES`). The screen limit is the one to quote for everyday use.
- **Optuna.** `pip install wattwise-lab` pulls in `trialforge-core[optuna]`, so the *Optuna* optimiser works after the normal install.
  If Optuna is missing from a hand-built environment, the option is hidden from the optimiser list instead of failing.
- **Tariff periods.** Periods must tile the 24 h day exactly for every month and weekday they apply to; gaps and overlaps (including across
  midnight) are reported and the tariff is not saved.
