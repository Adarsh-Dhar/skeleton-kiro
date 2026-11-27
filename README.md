# TRIALFORGE workspace

**TRIALFORGE** (`skeleton/packages/trialforge-core`, Python package `trialforge`) is a domain-independent SDK for planned experiments: you
describe factors, objectives and constraints; it generates a design (full/fractional factorial, OFAT, central composite, Box-Behnken, random),
runs it through an adapter, ingests and cleans measurements, analyses effects (ANOVA, interactions, confidence intervals), proposes better
settings (Bayesian optimisation), persists everything and writes reports.

The workspace holds two independent applications that share one database. The SDK contains no F1 or electricity knowledge (enforced by tests):

| App | Port | Domain | What it does |
|---|---|---|---|
| [**F1 SETUP LAB**](f1-setup-lab/README.md) | 8001 | Formula 1 | Two workspaces, built on the SDK: **Setup Engineering** (setup experiments on a synthetic vehicle model or imported telemetry) and **Historical F1** (real sessions imported from [OpenF1](https://openf1.org), free, 2023 onwards: laps, telemetry, track map, running order, results, gaps, pit stops, race control, team radio, weather) |
| [**WATTWISE LAB**](wattwise-lab/README.md) | 8002 | Household electricity | Household/EV model + simulator adapter, tariffs, smart-meter import and baseline, electricity screens, built on the SDK |

How both apps use the SDK: each builds an `ExperimentSpec`, registers an app-owned adapter in an `AdapterRegistry`, and calls `ExperimentService`
(design, execute through `LocalBackgroundRunner`, `ingest_measurements`, `analyse`, `optimise`, `report`). It imports only the SDK's public
top-level API, from the installed wheel, never from the source tree. The SDK is frozen: nothing in this release changes it.

**Build your own app on the SDK:** see [Use the TRIALFORGE SDK in your own app](#use-the-trialforge-sdk-in-your-own-app) below.

**Third-domain demonstration.** `scripts/demo-coffee-integration.sh` runs a coffee-roasting experiment through the installed SDK, using an
adapter registered in an `AdapterRegistry` and selected by `ExperimentSpec.adapter_name` (see
[skeleton/examples/third_domain_adapter](skeleton/examples/third_domain_adapter/README.md)). The roaster is synthetic, and the SDK has no
coffee-specific code.

## Install and run

Requires Python 3.11 or newer (`PYTHON_BIN` selects the interpreter). Each start script creates or reuses the app's virtual environment,
installs the SDK wheel (built if missing; a missing or failed install stops the start) and the root database package and the app, and checks the SDK version reported by `/api/health`, refuses an
occupied port with a clear message, migrates the root database and starts the server:

```bash
pnpm run start                 # BOTH apps at once (output prefixed [f1] / [wattwise]); Ctrl+C stops both
pnpm run stop                  # stop both, from another terminal
pnpm run status                # which of them is running
pnpm run start-f1              # F1 SETUP LAB only,  http://127.0.0.1:8001     F1_PORT to change
pnpm run start-wattwise        # WATTWISE LAB only,  http://127.0.0.1:8002     WATTWISE_PORT to change
```

`pnpm` (or `npm run <name>`) only runs the shell scripts listed in the root `package.json`; there is nothing to install. The same
commands work without it: `scripts/start-all.sh [start|stop|status|check]`, `scripts/start-f1.sh`, `scripts/start-wattwise.sh` (each
also accepts `stop`, `status` and `check`, a dry run of interpreter, wheel (WATTWISE), venv, port and database).
`pnpm run start` refuses to start if either port is taken and names the process holding it. If you are already running the apps, use
other ports: `F1_PORT=18001 WATTWISE_PORT=18002 pnpm run start`.

The two apps run as separate processes with separate virtual environments; either can run without the other.

## One database

Everything both apps store is in **one SQLite file at the repository root, `data/trialforge.db`** (set `TRIALFORGE_DB` to use another file).
The root package [`db/`](db) owns it: engine (WAL, `busy_timeout`, foreign keys), every app model (`db/models/`), ordered transactional
migrations (`python -m db.migrate [--status]`), and the app-scoped view of the SDK tables (`db/scoped.py`).

| Tables | Owner | Contents |
|---|---|---|
| `experiments`, `runs`, `measurements`, … | SDK (created by the unmodified `SQLiteExperimentRepository`) | WATTWISE and F1 setup experiments |
| `app_experiment_owner`, `app_schema_version` | `db` | which app owns each experiment (an app sees only its own); schema version |
| `f1_*` | F1 SETUP LAB | OpenF1 historical sessions (laps, telemetry, track location, weather, race datasets) and import jobs (`f1_hist_*`, `f1_import_job`) |
| `ww_*` | WATTWISE LAB | household settings, tariff library, historical meter imports and readings |

There are no other stores: no per-app databases, Parquet files, JSON tariff file, evidence files or browser storage. One thing outside the
database is not application data: the OpenF1 answer cache (`data/openf1-cache/`). WATTWISE ships no demo data; its synthetic sandbox pack
lives in `wattwise-lab/demo-sandbox/` and only ever uses its own database.

**Historical F1 analysis features** (details in the [F1 SETUP LAB README](f1-setup-lab/README.md)): championship standings, wind direction,
driver headshots and team colours, season comparison across imported sessions, mini-sector analysis, tyre degradation, and CSV/HTML export.
Headshots and team colours appear only for sessions imported after this change (re-import with force to refresh). Tyre slopes do not correct
for fuel burn. Mini-sector distance is an estimate.

**F1 SETUP LAB 2.1 has setup engineering and the historical data.** Migrations 9 and 10
(run automatically on start) restored and then removed the tables of a recorded-stint feature that no longer exists, and the database is first copied to `data/backups/<UTC time>/`. FastF1 is not supported:
migration 8 removed any FastF1 sessions and import jobs, and historical data comes from OpenF1 only. OpenF1 sessions, WATTWISE rows and the SDK
tables are kept.

**Upgrading WATTWISE from an earlier version** (with `wattwise.db`, `tariffs.json`):

```bash
python scripts/migrate_to_root_db.py --dry-run         # rehearse on a copy, change nothing
python scripts/migrate_to_root_db.py                   # back up to data/backups/<time>/, migrate, verify; legacy files kept
python scripts/migrate_to_root_db.py --delete-legacy   # ... then delete the legacy stores, only if verification passed
```

Re-running is safe. Household settings in a browser are uploaded the next time WATTWISE is opened in that browser.

## What is real and what is simulated

| Label | Where it comes from |
|---|---|
| F1 `HISTORICAL F1 · OPENF1` | real Formula 1 timing and telemetry from the free, unofficial OpenF1 API; values estimated from it are listed per session |
| F1 `SYNTHETIC` | everything the F1 Simulator produces: an invented vehicle model, not real telemetry |
| WATTWISE inputs | yours (bill, meter export, labels, vehicle documents) or a cited source, each with a source record; no built-in example household ([data sources](wattwise-lab/docs/DATA_SOURCES.md)) |
| WATTWISE `historical_measured` | your imported meter readings (`…_priced` = priced with the tariff you entered) |
| WATTWISE `projected` | model estimate for a schedule |
| WATTWISE `measured_schedule` | readings after actually running a schedule at home |

Savings are only real when measured.

## Use the TRIALFORGE SDK in your own app

TRIALFORGE knows nothing about F1, electricity or any other domain. You connect it to **your** system (a simulator, an
instrument, a lab process, a web API) by writing one **adapter**, and you describe each study as an **`ExperimentSpec`**. The SDK
then designs the runs, executes them through your adapter, cleans and stores the measurements, analyses the effects, proposes
better settings and writes reports. F1 SETUP LAB and WATTWISE LAB are built exactly this way. The walkthrough below builds a
small app from scratch. Every code block is a working file: `tests/test_readme_sdk_guide.py` extracts the blocks and runs them
against the installed SDK, so this section cannot drift from the real API.

### 1. Install the SDK into your project

Python 3.11 or newer. The SDK is the `trialforge-core` distribution (import name `trialforge`). Install the built wheel rather than
importing the source tree:

```bash
python -m venv .venv && . .venv/bin/activate
pip install /path/to/skeleton-kiro-main/skeleton/dist/trialforge_core-0.2.2-py3-none-any.whl
# or build the wheel first:  pip wheel --no-deps -w dist /path/to/skeleton-kiro-main/skeleton/packages/trialforge-core
pip install "optuna>=3.5"     # optional: enables the "optuna" optimiser
python -c "import trialforge; print(trialforge.__version__)"     # 0.2.2
```

Runtime dependencies are pydantic, SQLAlchemy, numpy and scipy, all installed with the wheel. Pin the version in your own
`pyproject.toml` (for example `trialforge-core>=0.2.2,<0.3`). Import only names from the package root (`from trialforge import ...`).
Deep imports such as `trialforge.analysis.engine` are internal and may change. WATTWISE has a test that enforces this rule. Copy it.

### 2. Lay out your app

```
my_app/
  __init__.py
  adapter.py       # connects TRIALFORGE to your system (the only domain-specific SDK code you write)
  experiment.py    # the ExperimentSpec and the design -> run -> analyse -> optimise -> report flow
  web.py           # optional: the same behind an HTTP API, running in the background
```

### 3. Write the adapter

An adapter is a plain class (a runtime-checkable protocol, so you don't subclass anything). It needs:

- `metadata`: its `name` must match `ExperimentSpec.adapter_name`. Set `synthetic=True` for simulated data, and reports will say so.
- `capabilities`: the engine trusts these, so be honest. `idempotent=True` means re-running the same `run_id` is safe and gives the same result. `resumable=True` means `status()` and `collect_result()` still work after a restart. `concurrent` and `max_concurrency` control parallel runs.
- `validate(config)`: raise `AdapterError` for configurations your system cannot run. These are never retried.
- `execute_run(ctx)`: does the work for one run and returns `ExecutionResponse(completed=True, measurements=[...])`. `ctx` holds `run_id`, `configuration`, `attempt`, `idempotency_key`, `random_seed` and `metadata["requested_metrics"]`.
- `prepare`, `execute`, `collect_result`, `status` and `cancel`: required by the protocol. Simple immediate adapters can stub them as below.
- **Delayed results** (lab tests that finish hours later): declare `delayed_measurements=True`, return `ExecutionResponse(completed=False, handle="...")`, answer `collect_result(run_id)` once data exist, and call `service.collect_pending(experiment_id)`.

Each `Measurement` carries `metric`, `value` and `unit` (convertible units such as `W`/`kW` or `ms`/`s` are converted to the
unit in the `MeasurementDefinition`), plus `source` (provenance) and `metadata["synthetic"]` for simulated values. Time series:
one `Measurement` per sample with a `timestamp`, and `time_series=True` on the definition. Never "fix" suspicious values in the
adapter: the measurement engine flags outliers, duplicates and unit mismatches, and the analysis decides what to use.

```python
# file: my_app/adapter.py
"""Connects TRIALFORGE to your system. Here: a SYNTHETIC bread oven (a made-up model, for illustration)."""
from __future__ import annotations

import hashlib
import random

from trialforge import (
    AdapterCapabilities,
    AdapterError,
    AdapterMetadata,
    ExecutionContext,
    ExecutionResponse,
    ExecutionStatusResponse,
    Measurement,
    MeasurementResponse,
)


class OvenAdapter:
    metadata = AdapterMetadata(
        name="bread-oven",                 # ExperimentSpec.adapter_name selects the adapter by this name
        version="1.0",
        domain="baking",
        synthetic=True,                    # True: the data is simulated, and reports say so
        description="Synthetic oven: crust, moisture and score for a bake setting",
        input_schema={"temperature": {"type": "continuous", "min": 160, "max": 260},
                      "minutes": {"type": "continuous", "min": 10, "max": 60}},
    )
    capabilities = AdapterCapabilities(immediate_execution=True, concurrent=True, max_concurrency=4,
                                       retryable=True, idempotent=True, resumable=False)

    async def validate(self, config: dict) -> None:
        """Reject what your system cannot run (never retried)."""
        if config["temperature"] * config["minutes"] > 14000:
            raise AdapterError("this combination would burn the loaf")

    async def prepare(self, context: ExecutionContext) -> None:
        return None

    async def execute_run(self, ctx: ExecutionContext) -> ExecutionResponse:
        """Called once per run, possibly concurrently. Talk to your instrument/simulator/API here."""
        t, m = float(ctx.configuration["temperature"]), float(ctx.configuration["minutes"])
        rng = random.Random(int(hashlib.sha256(ctx.run_id.encode()).hexdigest()[:12], 16))  # same run id -> same result
        crust = (t - 150) * m / 400 + rng.gauss(0, 0.2)
        moisture = 45 - 0.4 * m - 0.03 * (t - 200) + rng.gauss(0, 0.5)
        score = 10 - (crust - 6) ** 2 / 3 - (moisture - 33) ** 2 / 20 + rng.gauss(0, 0.2)
        ms = [Measurement(run_id=ctx.run_id, metric=k, value=round(v, 4), unit=u, source="oven-sim v1", metadata={"synthetic": True})
              for k, v, u in (("crust", crust, "index"), ("moisture", moisture, "percent"), ("score", score, "points"))]
        return ExecutionResponse(completed=True, measurements=ms)

    async def execute(self, config: dict) -> ExecutionResponse:          # legacy path, required by the protocol
        raise AdapterError("use execute_run")

    async def collect_result(self, run_id: str) -> MeasurementResponse:  # only for delayed measurements
        return MeasurementResponse(completed=False)

    async def status(self, handle: str) -> ExecutionStatusResponse:
        return ExecutionStatusResponse(state="unknown")

    async def cancel(self, run_id: str) -> None:
        return None
```

### 4. Describe the experiment, then run, analyse, optimise and report

| Spec field | What it holds |
|---|---|
| `factors` | what you vary: `Factor(name, factor_type="continuous" \| "discrete" \| "categorical", lower/upper or values, unit, default)`. The default is the control and baseline setting. |
| `objectives` | `Objective(metric, direction="maximize" \| "minimize", weight)`. Two or more objectives give Pareto analysis. |
| `constraints` | `Constraint(metric, kind="output", minimum/maximum)` marks runs infeasible (never recommended). `kind="input"` limits configurations. |
| `measurement_definitions` | one per metric: unit, `required`, expected range, `time_series`, outlier method |
| `design_method` + `design_options` | `factorial` (`continuous_levels`, `replicates`, `center_points`), `fractional_factorial` (`generators`), `ofat`, `central_composite`, `box_behnken`, `random` (`n_runs`) |
| `run_budget`, `random_seed` | upper limit on runs (designs and optimiser additions), and reproducibility |
| `execution` | `ExecutionPolicy(max_retries, max_concurrency, run_timeout_seconds)` |
| `metadata` | anything your app needs to keep with the study (WATTWISE stores the household and its input sources here) |

`ExperimentService` is the single entry point:

- `design(spec)` validates the spec and stores the plan.
- `run(spec)` designs and executes the plan.
- `execute(plan)` and `run_pending(id)` execute what is queued.
- `analyse(result)` returns effects, ANOVA, regression, feasible and infeasible runs, the best run and the Pareto front.
- `optimise(spec, result, count, method)` returns recommendations. Methods: `bayesian` (Gaussian process with Expected Improvement, constraint-aware), `optuna`, `random`, `grid`.
- `add_runs(id, configurations)` queues the recommendations. Alternatively, `run_sequential(id, iterations, batch_size)` runs the suggest-and-measure loop with stopping rules.
- `report(result, analysis)` returns a versioned report object.
- `ingest_measurements(id, run_id, measurements)` attaches results measured outside the adapter.
- Also available: `get`, `cancel`, `retry_run`, `recover`.

Pass a `SQLiteExperimentRepository("sqlite:///file.db")` to make everything persist across restarts. Without it the service uses
an in-memory database.

```python
# file: my_app/experiment.py
"""Describe, run, analyse, optimise and report one experiment."""
import asyncio

from trialforge import (
    AdapterRegistry,
    Constraint,
    ExperimentService,
    ExperimentSpec,
    Factor,
    MeasurementDefinition,
    Objective,
    ReportEngine,
    SQLiteExperimentRepository,
)

from my_app.adapter import OvenAdapter

spec = ExperimentSpec(
    experiment_id="bake-001",
    name="Best bake setting",
    hypothesis="A hotter, shorter bake scores higher without drying the loaf",
    adapter_name="bread-oven",
    factors=[
        Factor(name="temperature", factor_type="continuous", lower=190, upper=240, unit="degC", default=210),
        Factor(name="minutes", factor_type="continuous", lower=20, upper=45, unit="min", default=35),
    ],
    objectives=[Objective(metric="score", direction="maximize")],
    constraints=[Constraint(metric="moisture", kind="output", minimum=30)],  # runs below 30 % are infeasible
    measurement_definitions=[
        MeasurementDefinition(metric="score", unit="points"),
        MeasurementDefinition(metric="crust", unit="index"),
        MeasurementDefinition(metric="moisture", unit="percent"),
    ],
    design_method="factorial",
    design_options={"continuous_levels": 3, "replicates": 2},
    run_budget=40,
    random_seed=42,
)


async def main():
    registry = AdapterRegistry()
    registry.register(OvenAdapter())
    service = ExperimentService(registry=registry, repository=SQLiteExperimentRepository("sqlite:///my_app.db"))

    result = await service.run(spec)                       # design + execute every planned run, persisted
    print("runs:", len(result.runs), "completed:", sum(r.status.value == "completed" for r in result.runs))

    analysis = service.analyse(result)
    best = next(r for r in result.runs if r.run_id == analysis.best_run_id)
    print("best feasible run:", best.configuration)
    reg = next(r for r in analysis.regression if r.metric == "score")      # coded -1..+1: 2 x estimate = low-to-high change
    for c in reg.coefficients[1:]:
        print(f"effect on score: {c.term:22} {2 * c.estimate:+.2f}  p={c.p_value:.3f}")
    print("infeasible runs (moisture < 30 %):", len(analysis.infeasible_runs))

    recs = service.optimise(spec, result, count=2, method="bayesian")
    for r in recs:
        print("suggest:", {k: round(v, 1) for k, v in r.configuration.items()}, "-", r.rationale[:70])
    service.add_runs(spec.experiment_id, [r.configuration for r in recs], iteration=1, source="optimizer:bayesian")
    result = await service.run_pending(spec.experiment_id)  # measure the suggestions
    print("after optimisation:", len(result.runs), "runs")

    report = service.report(result, service.analyse(result))
    open("bake-001-report.json", "w").write(report.model_dump_json(indent=2))
    open("bake-001-runs.csv", "w").write(ReportEngine.to_csv(result))
    print("report written; best:", report.best_configuration)


if __name__ == "__main__":
    asyncio.run(main())
```

Run it with `python -m my_app.experiment`. It prints the 18 designed runs (3 × 3 levels × 2 replicates), the best **feasible**
setting, the effect of each factor on the score with its p-value (the regression is coded −1…+1, so `2 × estimate` is the change
from the low to the high end of the range), how many runs broke the moisture constraint, two Bayesian suggestions, and the run
count after measuring them. It writes `bake-001-report.json` and `bake-001-runs.csv`, and `ReportEngine.measurements_csv` and
`effects_csv` export the other tables. Running it twice gives identical designs and results, because the seed and the run ids
are deterministic.

### 5. Serve it from a web app (background execution)

`service.run()` blocks until every run is done. In a server, design first and hand execution to a `LocalBackgroundRunner`, then
poll `service.get(id)`. On start-up, `service.recover()` resumes or retries runs interrupted by a crash, depending on what the
adapter declares (`idempotent`, `resumable`). This is how both apps here work (FastAPI + `LocalBackgroundRunner`).

```python
# file: my_app/web.py
"""The same experiment behind a small FastAPI app, executed in the background."""
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from trialforge import AdapterRegistry, ExperimentService, LocalBackgroundRunner, Measurement, SQLiteExperimentRepository

from my_app.adapter import OvenAdapter
from my_app.experiment import spec

registry = AdapterRegistry()
registry.register(OvenAdapter())
service = ExperimentService(registry=registry, repository=SQLiteExperimentRepository("sqlite:///my_app.db"))
runner = LocalBackgroundRunner(service)


@asynccontextmanager
async def lifespan(app):
    await service.recover()          # after a crash/restart: resume or retry interrupted runs (as capabilities allow)
    yield
    await runner.shutdown()


app = FastAPI(lifespan=lifespan)


@app.post("/experiments/{eid}")
def create(eid: str):
    plan = service.design(spec.model_copy(update={"experiment_id": eid}))
    return {"id": eid, "planned_runs": len(plan.runs)}


@app.post("/experiments/{eid}/execute", status_code=202)
async def execute(eid: str):         # async: the runner needs the running event loop
    runner.submit(eid)               # returns at once; runs execute in the background
    return {"started": True}


@app.get("/experiments/{eid}")
def status(eid: str):
    try:
        res = service.get(eid)
    except KeyError:
        raise HTTPException(404) from None
    counts = {}
    for r in res.runs:
        counts[r.status.value] = counts.get(r.status.value, 0) + 1
    return {"status": res.status.value, "runs": counts}


@app.post("/experiments/{eid}/runs/{run_id}/measurements")
async def ingest(eid: str, run_id: str, body: dict):
    """Results measured outside the adapter (lab sheet, instrument export) for a run that has not been executed."""
    ms = [Measurement(run_id=run_id, metric=k, value=float(v["value"]), unit=v["unit"], source=body.get("source", "manual"))
          for k, v in body["values"].items()]
    run = await service.ingest_measurements(eid, run_id, ms)
    return {"run": run_id, "status": run.status.value}
```

`uvicorn my_app.web:app` starts it. `POST /experiments/<id>` designs, `POST .../execute` starts the background runs, and
`GET /experiments/<id>` shows progress. The `measurements` route records a run measured by hand: the run is completed with
those values and keeps their `source`.

### 6. Test your adapter, and avoid the common mistakes

- **Contract tests:** copy `skeleton/packages/trialforge-core/tests/contracts/test_adapter_contract.py` into your tests and add your adapter. It checks protocol conformance, metadata, validation, units, determinism, concurrency and a full experiment through the service.
- **Read the adapter guide** ([skeleton/docs/adapter_development/guide.md](skeleton/docs/adapter_development/guide.md)) for delayed measurements, plug-ins (derived metrics, transformations, custom objectives, constraints, optimisers and report sections, registered by name in a `PluginRegistry`) and custom units.
- **One adapter per configuration:** if every experiment needs its own adapter (for example its own household, as in WATTWISE), register it under a unique name such as `my-app/<experiment id>` and set `adapter_name` to match. Re-register the adapters at start-up before calling `recover()`.
- **`execute_run` is called concurrently.** Keep per-run state in local variables, or declare `concurrent=False`.
- **A constraint metric must be measured.** Output constraints and objectives refer to metrics in `measurement_definitions`, and runs missing a `required` metric fail when `strict_required_metrics=True`, which is the default.
- **Honesty is part of the contract:** mark simulated data `synthetic`, keep `source` on every measurement, and present a model's output as a prediction. Optimiser recommendations are labelled `prediction_kind="model_prediction"`; measure them before you claim a result.
- **Limits:** see [skeleton/docs/limitations.md](skeleton/docs/limitations.md) (sample sizes, model assumptions, what the statistics can and cannot show).

## Release check

```bash
scripts/check-release.sh
```

Works from a clean checkout (needs Python 3.11+ and access to PyPI). It creates its own environment, `.run/release-venv` (recreated on every
run; `--reuse` keeps it), builds the TRIALFORGE wheel into `skeleton/dist/`, installs that wheel with its test extras, the root database
package and both apps with their test dependencies, and checks that WATTWISE uses the SDK from that environment's `site-packages` (not the
source tree) and that F1 SETUP LAB also uses the installed SDK and does not depend on FastF1. It then builds the other packages, lints, compiles every Python file, checks JavaScript and shell syntax, runs the SDK, root,
F1 and WATTWISE test suites and the browser tests (when Playwright's Chromium is installed; `INSTALL_CHROMIUM=1` downloads it), and prints
PASSED / FAILED / SKIPPED / UNAVAILABLE separately. Opt-in: `RUN_NETWORK_TESTS=1` (real OpenF1 imports of five sessions from 2023 to 2026,
no account; takes several minutes), `RUN_START_SMOKE=1` (startup smoke, below). The real-household check is always listed as a SKIPPED manual
gate. Skipped and unavailable checks are not passes.

**Startup smoke (`RUN_START_SMOKE=1`).** Starts F1 SETUP LAB and WATTWISE LAB with the official `scripts/start-f1.sh` and
`scripts/start-wattwise.sh` on `F1_PORT`/`WATTWISE_PORT` (default 8001/8002) against a throw-away database. It reports each step
separately:

* each app answers `/api/health` with HTTP 200 and the right app name (WATTWISE also with the installed SDK version);
* the server pid owns the port;
* both apps are healthy together;
* the shared database is created and migrated;
* WATTWISE keeps running after F1 stops;
* both ports are free afterwards.

A port already in use (for example by apps you are running yourself) is reported as UNAVAILABLE and that process is never touched. An exit
trap stops anything the check started, whether it succeeds, fails or is interrupted. The first start installs each app's `.venv`; allow a
few minutes, or raise `STARTUP_TIMEOUT` (seconds per app, default 900).

```bash
RUN_START_SMOKE=1 scripts/check-release.sh                                         # ports 8001/8002 must be free
RUN_START_SMOKE=1 F1_PORT=18001 WATTWISE_PORT=18002 scripts/check-release.sh       # if you keep the apps running
```

## Kiro configuration

The SDK's spec, six steering files, five hooks and six owner agents live in `skeleton/.kiro/`. Kiro reads `.kiro` only at the workspace
root, so the repository root has a generated `.kiro/` too:

* **Hooks:** the same five hooks, matching only files under `skeleton/`.
* **Agents:** the same six agents, with `skeleton/`-prefixed paths. They load the SDK steering through `resources`.
* **Steering:** one file, `trialforge-sdk.md`, that pulls in the SDK steering only when you work on `skeleton/` files.

Ownership is defined once, in `skeleton/.kiro/ownership.json`. After editing it, run `python scripts/sync_kiro_config.py`.
`tests/test_kiro_config.py` fails if either copy drifts, if an SDK file has no owner or two owners, or if a hook matcher stops matching the
legacy patterns. [`skeleton/.kiro/agents/README.md`](skeleton/.kiro/agents/README.md) has the ownership map, the handoff protocol and the
multi-agent test results, including what can only be confirmed inside the Kiro IDE.

## Manual acceptance gates

These need you, your data or your bill. The code prepares them but cannot complete them truthfully:

* **WATTWISE:** import a real smart-meter export (7–30 days, 15/30-minute intervals; supply a sample header if the format is not recognised);
  enter your real tariff and household/EV parameters; compare the computed cost of a billed period with the bill; run a recommended schedule at
  home and import those readings to the run before claiming a saving.

## Limitations

SQLite only (one file, WAL; fine for one user and two app processes, not a shared server); no authentication (out of scope); single-process
servers; the household model is deliberately simple; historical F1 data depends on what OpenF1 publishes (2023 onwards); tariff and meter
formats are generic until configured from your own bill and export; statistical limits are those of the SDK (`skeleton/docs/limitations.md`).

## Layout

```
db/                 root database package (models, migrations, engine, app scoping)
data/trialforge.db  the database (created on first start; not committed)
scripts/            start scripts, release check, legacy migration
tests/              root tests (database, migrations, scoping, start scripts, legacy migration)
skeleton/           the TRIALFORGE SDK (frozen) and its docs
f1-setup-lab/       F1 SETUP LAB (setup engineering, historical F1 data from OpenF1)
wattwise-lab/       WATTWISE LAB
```
