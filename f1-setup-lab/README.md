# F1 SETUP LAB

A browser-based **design-of-experiments workstation** for vehicle setup, built entirely on the **TRIALFORGE** SDK
(`trialforge-core`, in [`../skeleton`](../skeleton)). Define setup variables, a lap-time objective and a stability
constraint; generate a fractional-factorial screening design; run it; see which factors matter (ANOVA, interactions, confidence
intervals); let a Bayesian optimiser refine the setup; export a justified recommendation. No Python or command line is needed to *use* it.

The app works with **three data sources**, kept clearly labelled everywhere:

| Mode | Source | Label | What it is |
|---|---|---|---|
| **Synthetic** (default) | Built-in vehicle model | `SYNTHETIC` | An invented response surface. Not real F1 telemetry. |
| **Live simulator** | EA SPORTS F1 25 UDP telemetry | `SIM-RECORDED` | You drive stints in the game; the app records laps and verifies the game reported exactly the requested setup. |
| **Historical F1** | [FastF1](https://github.com/theOehrly/FastF1) (unofficial) | `HISTORICAL F1 · FASTF1` | Real Grand Prix sessions (timing, telemetry, position, weather) imported for analysis and benchmarking &mdash; never mixed into setup experiments. |

![Analysis](docs/screenshots/06-analysis.png)

## Run it

Requires Python 3.11+. The app depends on the frozen SDK wheel, installed from the sibling `skeleton` directory:

```bash
cd f1-setup-lab
python -m venv .venv && . .venv/bin/activate
pip install ../skeleton/dist/trialforge_core-0.2.1-py3-none-any.whl
pip install -e .
f1-setup-lab                       # or: python -m f1lab --port 8001 --db data/f1lab.db
```

Open <http://127.0.0.1:8001>. State lives in one SQLite file (`data/f1lab.db`); restart the server and everything is still there
(runs interrupted by a crash are recovered automatically, see below). Interactive API docs: <http://127.0.0.1:8001/api/docs>.

## The workflow (all in the browser)

| # | Screen | What you do | Screenshot |
|---|---|---|---|
| 1 | **Dashboard** | See every experiment, progress, best feasible lap | [10](docs/screenshots/10-dashboard.png) |
| 2 | **New experiment** | Pick factors and ranges (front wing, rear wing, suspension, tyre pressure), minimise lap time, minimum stability, design method, budget | [02](docs/screenshots/02-experiment-creator.png) |
| 3 | **Setup design** | Inspect the generated setups: 2<sup>4-1</sup> fractional factorial, resolution IV, `I=ABCD`, alias structure, randomised order | [03](docs/screenshots/03-setup-design.png) |
| 4 | **Run monitor** | Run on the simulator, watch results arrive; cancel, retry failed runs; infeasible setups flagged | [04](docs/screenshots/04-run-monitor.png) |
| 5 | **Telemetry** | Lap/sector charts per run; import a recorded CSV (s or ms) with unit conversion and outlier flagging; attach it to a queued run | [05](docs/screenshots/05-telemetry.png), [13](docs/screenshots/13-telemetry-import.png) |
| 6 | **Analysis** | Factor effects with 95% CI, Type II ANOVA, interactions, feasibility plot, written justification | [06](docs/screenshots/06-analysis.png) |
| 7 | **Optimisation** | Preview and run Bayesian-optimisation proposals; convergence chart | [07](docs/screenshots/07-optimisation-preview.png), [08](docs/screenshots/08-optimisation-result.png) |
| 8 | **Report** | Printable HTML report, JSON, CSV exports, reproducibility facts | [09](docs/screenshots/09-report.png) |
| 9 | **Live simulator** | Listen for EA SPORTS F1 25 UDP telemetry, watch live speed/throttle/brake/tyre charts, record verified stints | — |
| 10 | **Historical F1** | Browse seasons/events, import real sessions via FastF1, analyse laps, telemetry, track map, weather and driver comparisons | — |

Demo datasets: `data/demo_telemetry.csv` (seconds) and `data/demo_telemetry_ms.csv` (milliseconds): 8 laps, one deliberately slow lap
that gets flagged. Both load from the Telemetry screen ("Load demo"). Failures are easy to demo: set *Demo fault rate* to 1 and *Retries* to 0
on the creator form (screenshot [12](docs/screenshots/12-failed-runs.png)); invalid input is explained inline ([11](docs/screenshots/11-validation-error.png)).

## How it uses the SDK (and what it does *not* contain)

The application contains **only** F1 knowledge: the vehicle model, the simulator adapter, telemetry CSV parsing, and the screens.
All experiment machinery comes from `trialforge`:

| App feature | TRIALFORGE API |
|---|---|
| Experiment definition, constraints | `ExperimentSpec`, `Factor`, `Objective`, `Constraint`, `MeasurementDefinition`, `ExecutionPolicy` |
| Fractional factorial / CCD / OFAT designs, alias structure | `ExperimentService.design` &rarr; `ExperimentDesigner` |
| Simulator | an app-owned adapter implementing the SDK protocol (`f1lab/adapters/sim_adapter.py`), registered in `AdapterRegistry` |
| Background execution, retries, cancel, restart recovery | `LocalBackgroundRunner`, `ExperimentService.execute / cancel / retry_run / recover` |
| Telemetry import, unit conversion, outlier flags | `MeasurementEngine`, `ExperimentService.ingest_measurements` |
| Effects, ANOVA, interactions, feasibility-aware best run | `ExperimentService.analyse` &rarr; `AnalysisEngine` |
| Optimisation (GP + Expected Improvement, constraint-aware) | `ExperimentService.optimise / add_runs` &rarr; `Optimizer` |
| Persistence, history, reports, CSV exports | `SQLiteExperimentRepository`, `ExperimentService.report`, `ReportEngine` |

Code map: `backend/f1lab/adapters` (vehicle model, simulator adapter, telemetry), `backend/f1lab/services` (`LabService`, narrative, HTML report),
`backend/f1lab/api` (FastAPI routes/schemas), `backend/f1lab/integrations/game_udp` (F1 25 / 2026-season UDP receiver, decoders, stint recorder),
`backend/f1lab/integrations/historical` (FastF1 client, importer, normalizer, parquet repository, service), `frontend/` (no-build single-page app
with its own SVG charts). The app imports `trialforge` only through its public top-level API; a skeleton-level proof test enforces this.

## Live simulator mode (EA SPORTS F1 25)

Open **Live simulator** in the nav (or "New experiment &rarr; Live simulator"). In the game's telemetry settings enable UDP telemetry
(F1 25 or F1 25: 2026 Season Pack format), point it at the shown IP/port, then press **Start listening**. The **Live telemetry** charts
update from the decoded stream (speed, throttle/brake, steering, RPM, gear, clutch, DRS, rev lights, tyre temperatures/pressures,
track surface). For experiments, each generated setup is driven by you in the game garage; a stint is accepted only if the game
reported exactly the target setup for the whole stint, and the decoded telemetry is archived with the run as evidence.

## Historical F1 mode (FastF1)

Open **Historical F1**. Pick a season and Grand Prix, choose a session (FP1&ndash;FP3, Q, SQ, S, R), optionally select drivers, and import.
FastF1 downloads the session (coverage starts in 2018; data appears after a session finishes), the importer normalizes laps, car telemetry,
track position and weather into Parquet under `data/historical/`, and the session screens provide lap/sector analysis, telemetry traces,
a track map with corner markers, weather/tyre-stint views and driver-vs-driver comparisons with an interpolated time delta.
Lap validity follows the timing feed: laps deleted by the stewards (`Deleted`) are marked invalid, while `IsAccurate` (timing alignment
quality) is stored separately and never treated as a deletion. The car `Brake` channel is published by FastF1 as a boolean
"brakes applied" flag, so it is stored as `brake_applied` (0/100), not as a brake-pressure percentage.

FastF1 is an unofficial community library and is not affiliated with Formula 1; its timing feed sometimes requires F1 account
credentials (`F1_USERNAME`/`F1_PASSWORD` environment variables) for recent sessions.

## Reading the results honestly

* The recommended setup is always the best **measured, feasible** run. Runs violating the stability constraint are never recommended, even if faster,
  and the report states what the constraint costs.
* Bar charts show the *linear* low-to-high effect; the ANOVA treats levels as categories and also detects curvature, so a factor can differ between the two.
* In a fractional design, two-factor interactions are aliased in pairs; the design screen and analysis say which.
* Optimisation results vary. With the bundled model, three reproducible demo experiments (ids `opt-loop-1..3`) improved the best feasible lap by
  0.38 s, 0.55 s and 0.00 s after 12 optimiser runs. The model-based loop does not guarantee improvement in a handful of runs.
* Simulated runs are reproducible: results are seeded from the experiment id, so set `experiment_id` in the API (or reuse an id) for identical data.

## Tests

```bash
pip install -e ".[test]"
python -m playwright install chromium     # only for the browser tests
python -m pytest                          # API/backend tests + real-browser end-to-end (skipped without Chromium)
```
Backend tests cover validation, constraint handling, failed runs and retries, cancellation, telemetry import, optimisation, reports, restart persistence,
the UDP receiver/decoders and stint recording, and the historical importer/normalizer/repository against a fake FastF1 session. The browser tests walk
the whole workflow with the mouse and regenerate `docs/screenshots/`.

## Limitations

Single-process server (the SDK's local background runner); SQLite only; no user accounts; historical sessions are analysis-only and contain no car
setup configuration; FastF1 coverage/accuracy varies by season and the live mode depends on the game's UDP output being enabled; the synthetic
vehicle model is deliberately simple; statistical limits are those of the SDK (see `../skeleton/docs/limitations.md`).
