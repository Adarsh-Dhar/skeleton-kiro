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

There are no other stores: no per-app databases, Parquet files, JSON tariff file, evidence files or browser storage. Two things outside the
database are not application data: the OpenF1 answer cache (`data/openf1-cache/`) and WATTWISE's bundled demo CSVs.

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
| WATTWISE `synthetic` | the built-in example household |
| WATTWISE `historical_measured` | your imported meter readings (`…_priced` = priced with the tariff you entered) |
| WATTWISE `projected` | model estimate for a schedule |
| WATTWISE `measured_schedule` | readings after actually running a schedule at home |

Savings are only real when measured.

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
