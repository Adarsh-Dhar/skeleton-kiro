# TRIALFORGE workspace

**TRIALFORGE** (`skeleton/packages/trialforge-core`, Python package `trialforge`) is a domain-independent SDK for planned experiments: you
describe factors, objectives and constraints; it generates a design (full/fractional factorial, OFAT, central composite, Box-Behnken, random),
runs it through an adapter, ingests and cleans measurements, analyses effects (ANOVA, interactions, confidence intervals), proposes better
settings (Bayesian optimisation), persists everything and writes reports.

Two independent applications prove the SDK is reusable across unrelated domains. Neither contains experiment machinery of its own, and the SDK
contains no F1 or electricity knowledge (enforced by tests):

| App | Port | Domain | What it adds on top of the SDK |
|---|---|---|---|
| [**F1 SETUP LAB**](f1-setup-lab/README.md) | 8001 | Racing-car setup | Vehicle model + simulator adapter, EA SPORTS F1 25 UDP receiver (live stints), FastF1 historical sessions, F1 screens |
| [**WATTWISE LAB**](wattwise-lab/README.md) | 8002 | Household electricity | Household/EV model + simulator adapter, tariffs, smart-meter import and baseline, electricity screens |

How each uses the SDK: both build an `ExperimentSpec`, register an app-owned adapter in an `AdapterRegistry`, and call `ExperimentService`
(design, execute through `LocalBackgroundRunner`, `ingest_measurements`, `analyse`, `optimise`, `report`). Both import only the SDK's public
top-level API, from the installed wheel, never from the source tree. The SDK is frozen: nothing in this release changes it.

## Install and run

Requires Python 3.11 or newer (`PYTHON_BIN` selects the interpreter). Each start script builds the SDK wheel if it is missing, creates or reuses
the app's virtual environment, installs the SDK wheel, the root database package and the app, checks the installed `trialforge-core` version,
refuses an occupied port with a clear message, migrates the root database and starts the server:

```bash
scripts/start-f1.sh            # http://127.0.0.1:8001     F1_PORT to change
scripts/start-wattwise.sh      # http://127.0.0.1:8002     WATTWISE_PORT to change
scripts/start-f1.sh stop       # also: status, check (dry run: interpreter, wheel, venv, port, database)
```

The two apps run as separate processes with separate virtual environments; either can run without the other.

## One database

Everything both apps store is in **one SQLite file at the repository root, `data/trialforge.db`** (set `TRIALFORGE_DB` to use another file).
The root package [`db/`](db) owns it: engine (WAL, `busy_timeout`, foreign keys), every app model (`db/models/`), ordered transactional
migrations (`python -m db.migrate [--status]`), and the app-scoped view of the SDK tables (`db/scoped.py`).

| Tables | Owner | Contents |
|---|---|---|
| `experiments`, `runs`, `measurements`, … | SDK (created by the unmodified `SQLiteExperimentRepository`) | experiments of both apps |
| `app_experiment_owner`, `app_schema_version` | `db` | which app owns each experiment (each app sees only its own); schema version |
| `f1_*` | F1 SETUP LAB | historical sessions, import jobs, raw UDP packets, stint evidence and telemetry, probe reports, mapping confirmations |
| `ww_*` | WATTWISE LAB | household settings, tariff library, historical meter imports and readings |

There are no other stores: no per-app databases, Parquet files, JSON tariff file, evidence files or browser storage. Two things outside the
database are not application data: FastF1's download cache (`data/fastf1-cache/`) and the bundled demo CSVs.

**Upgrading from an earlier version** (with `f1lab.db`, `wattwise.db`, Parquet sessions, evidence files, `.f1raw` captures, `tariffs.json`):

```bash
python scripts/migrate_to_root_db.py --dry-run         # rehearse on a copy, change nothing
python scripts/migrate_to_root_db.py                   # back up to data/backups/<time>/, migrate, verify; legacy files kept
python scripts/migrate_to_root_db.py --delete-legacy   # ... then delete the legacy stores, only if verification passed
```

Re-running is safe. Household settings in a browser are uploaded the next time WATTWISE is opened in that browser.

## What is real and what is simulated

| Label | Where it comes from |
|---|---|
| F1 `SYNTHETIC` | an invented vehicle model |
| F1 `SIM-RECORDED` | your own laps in EA SPORTS F1 25 (a video game), decoded from its UDP output |
| F1 `HISTORICAL F1 · FASTF1` | real Formula 1 timing/telemetry via the unofficial FastF1 library; analysis only, never an experiment result |
| WATTWISE `synthetic` | the built-in example household |
| WATTWISE `historical_measured` | your imported meter readings (`…_priced` = priced with the tariff you entered) |
| WATTWISE `projected` | model estimate for a schedule |
| WATTWISE `measured_schedule` | readings after actually running a schedule at home |

Savings are only real when measured. Setup-field mappings are only verified when you confirm them from the game screen.

## Release check

```bash
scripts/check-release.sh
```

Works from a clean checkout (needs Python 3.11+ and access to PyPI). It creates its own environment, `.run/release-venv` (recreated on every
run; `--reuse` keeps it), builds the TRIALFORGE wheel into `skeleton/dist/`, installs that wheel with its test extras, the root database
package and both apps with their test dependencies, and checks that both apps use the SDK from that environment's `site-packages` (not the
source tree). It then builds the other packages, lints, compiles every Python file, checks JavaScript and shell syntax, runs the SDK, root,
F1 and WATTWISE test suites and the browser tests (when Playwright's Chromium is installed; `INSTALL_CHROMIUM=1` downloads it), and prints
PASSED / FAILED / SKIPPED / UNAVAILABLE separately. Opt-in: `RUN_NETWORK_TESTS=1` (real FastF1 download, needs `F1_USERNAME` /
`F1_PASSWORD`), `RUN_START_SMOKE=1` (full install and start of both apps). The real-game and real-household checks are always listed as
SKIPPED manual gates. Skipped and unavailable checks are not passes.

## Manual acceptance gates

These need you, your game, your data or your bill. The code prepares them but cannot complete them truthfully:

* **F1:** enable UDP output in EA SPORTS F1 25 and confirm packets arrive; probe all four setup factors at low and high values and record the
  decision per game variant (`scripts/probe_setup.py --guided`, `scripts/verify_setup_mapping.py`); drive a baseline stint and the experiment
  stints; set `F1_USERNAME`/`F1_PASSWORD` and import a real FastF1 session (`scripts/smoke_fastf1_import.py`).
* **WATTWISE:** import a real smart-meter export (7–30 days, 15/30-minute intervals; supply a sample header if the format is not recognised);
  enter your real tariff and household/EV parameters; compare the computed cost of a billed period with the bill; run a recommended schedule at
  home and import those readings to the run before claiming a saving.

## Limitations

SQLite only (one file, WAL; fine for one user and two app processes, not a shared server); no authentication (out of scope); single-process
servers; the vehicle and household models are deliberately simple; historical F1 data depends on FastF1 and an F1 account; tariff and meter
formats are generic until configured from your own bill and export; statistical limits are those of the SDK (`skeleton/docs/limitations.md`).

## Layout

```
db/                 root database package (models, migrations, engine, app scoping)
data/trialforge.db  the database (created on first start; not committed)
scripts/            start scripts, release check, legacy migration
tests/              root tests (database, scoping, start scripts, migration)
skeleton/           the TRIALFORGE SDK (frozen) and its docs
f1-setup-lab/       F1 SETUP LAB
wattwise-lab/       WATTWISE LAB
```
