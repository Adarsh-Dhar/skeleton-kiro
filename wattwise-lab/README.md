# WATTWISE LAB

A browser-based **experiment workstation for household electricity**, built entirely on the **TRIALFORGE** SDK
(`trialforge-core`, in [`../skeleton`](../skeleton)). Describe your tariff, your electric car and the appliances you can schedule; choose which
parts of the evening schedule to vary; measure every combination on a simulated household; see which choices move the bill; let a Bayesian
optimiser look for the cheapest schedule that **still leaves the car charged by the time it leaves**. No Python or command line is needed to *use* it.

> **SIMULATED DATA.** Household load, EV charging and meter readings come from a simple model. Nothing is measured from a real home unless
> you import your own CSV, and no tariff API, meter or vehicle is contacted. Every screen carries a banner saying so, and every report repeats it.

## Run it

Requires Python 3.11+. The app depends on the frozen SDK wheel, installed from the sibling `skeleton` directory:

```bash
cd wattwise-lab
python -m venv .venv && . .venv/bin/activate
pip install ../skeleton/dist/trialforge_core-0.2.1-py3-none-any.whl
pip install -e .
wattwise-lab                       # or: python -m wattwise --port 8002 --db data/wattwise.db
```

Open <http://127.0.0.1:8002>. State lives in one SQLite file (`data/wattwise.db`) plus `data/tariffs.json` for your saved tariffs; restart the
server and everything is still there (runs interrupted by a crash are recovered automatically). Interactive API docs: <http://127.0.0.1:8002/api/docs>.
It runs independently of F1 SETUP LAB (port 8001); both can be installed in the same or separate environments.

## The workflow (all in the browser)

| Screen | What you do |
|---|---|
| **Energy dashboard** | See a typical day against your tariff (load stacked over price bands), plus every experiment, its progress and the saving found |
| **Household setup** | Edit tariffs (periods that wrap midnight, demand charge; gaps and overlaps are explained as you type), the EV (battery, charge needed and departure time) and appliances |
| **Experiment designer** | Pick the variables (EV charging start, charger power, flexible-appliance start), their ranges, the design method (full / fractional factorial, one-factor-at-a-time, central composite, Box-Behnken, random) and a run budget |
| **Schedule simulator** | Move three sliders and watch the day re-priced against today's schedule: cost, kWh, peak kW, charge at departure, cost per price period |
| **Run monitor** (inside an experiment) | Run the design, watch progress, cancel, retry failed runs; open any run's day; attach a smart-meter CSV to a run |
| **Analysis** | Cost of every schedule, cost against peak demand, factor effects with 95% intervals, interactions, Type II ANOVA, and which schedules failed the departure-charge constraint |
| **Optimisation** | Compare the best schedule with today's day, ask for suggestions (Bayesian, Optuna, random, grid), run them, follow the history |
| **Reports** | Printable HTML report, JSON, CSV exports of schedules, measurements and effects |
| **Meter import** | Preview a `timestamp,power_kw` (or `power_w`) CSV, priced with a chosen tariff; quality problems are listed, not hidden |

Bundled demo files: `data/demo_meter.csv` (kW) and `data/demo_meter_w.csv` (W), one evening-to-evening day at 15-minute resolution. Failures are
easy to demo: set *Demo fault rate* to 1 and *Retries* to 0 in the designer.

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
| Persistence, history, reports, CSV exports | `SQLiteExperimentRepository`, `ExperimentService.report`, `ReportEngine` |

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
* Costs are for one simulated day on the tariff you entered; real savings depend on real usage and your supplier's billing rules.
