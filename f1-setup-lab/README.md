# F1 SETUP LAB

F1 SETUP LAB has three separate workspaces: **Setup Engineering**, **Live Simulator** and **Historical F1**. Data from each stays apart and is
labelled by source. The app depends on the TRIALFORGE SDK, FastF1 is not supported, and all historical data comes from OpenF1.

## The three workspaces

### Setup Engineering

Plan and analyse car-setup experiments. Define factors (front wing, rear wing, and so on), generate a design, run it on the synthetic vehicle
model or import stint telemetry from a CSV file, then see effects, a recommendation, optimisation suggestions and a report. **All data the
Simulator produces is labelled SYNTHETIC**: the vehicle model is an invented response surface, not real Formula 1 telemetry. Screens:
Dashboard, New experiment, Telemetry import.

### Live Simulator

Drive stints in EA SPORTS F1 25 (F1 25 or 2026 Season Pack UDP format) and record the game's UDP telemetry as setup-experiment measurements.
A stint is accepted only when the setup read from the game matches the target setup. Results are labelled as simulator-recorded video-game
data. **Live UDP capture needs the game to be running and sending packets** (default `127.0.0.1:20777`). Screen: Live simulator.

### Historical F1

Import and analyse **real historical Formula 1 sessions** from [OpenF1](https://openf1.org) (free, no account, 2023 onwards): laps,
telemetry, track map, comparisons, running order, results, gaps, pit stops, race control, team radio and weather. Everything is labelled
`HISTORICAL F1 · OPENF1` and is used for analysis only; it never becomes a setup-experiment result. Screens: Historical F1, Imported sessions.

## SDK dependency

F1 SETUP LAB depends on the **TRIALFORGE SDK** (`trialforge-core>=0.2.0,<0.3`, installed from the SDK wheel by `scripts/start-f1.sh`). The
SDK provides the experiment machinery for Setup Engineering and the Live Simulator: design generation, run execution, measurement ingestion,
analysis, optimisation and reports. F1 knowledge (the vehicle model, game catalogue, UDP decoding) lives in this app, not in the SDK.

## Data sources, FastF1 and the game

* **FastF1 is not supported.** It is not a dependency and no code imports it. Historical data comes from OpenF1 only.
* **Simulator data is SYNTHETIC.** Live Simulator data is video-game data, and historical data is real OpenF1 data. The three are never mixed.
* **Live UDP capture requires the running game.** Everything below was verified with recorded packets, not the live game.

### Verified only with recorded packets (not tested against the live game)

These behaviours were tested with recorded or constructed packets. They have **not been tested against the live game**:

* F1 25 packet decoding (packet sizes, setup, telemetry and session history)
* 2026 Season Pack packet decoding
* receiver packet counting (good, malformed and wrong-format packets)
* live stint recording and top-speed tracking
* stint acceptance against the target setup, including rejection of an out lap, invalid laps, too few valid laps and inconsistent setups
* session change during a stint (the stint is rejected and state is reset)
* raw UDP recording and replay from the database
* legacy `.f1raw` file import, including a truncated tail
* setup-field probing (`scripts/probe_setup.py`) and mapping verification (`scripts/verify_setup_mapping.py`)
* UDP recording inspection (`scripts/inspect_udp_recording.py`)

The four setup-factor mappings stay unverified until you confirm them against the game's garage screen with `verify_setup_mapping.py confirm`.

Historical data is labelled `HISTORICAL F1 · OPENF1`. OpenF1 is unofficial and not affiliated with Formula 1. Some values are estimated
from what OpenF1 publishes (lap distance, for example); each session's *Import coverage* panel lists what was provided, estimated and missing.

## Run it

Requires Python 3.11+. The simplest way is the start script at the repository root (`scripts/start-f1.sh` or `pnpm run start-f1`, see the
root README): it creates `f1-setup-lab/.venv`, installs the SDK wheel and everything else and starts the server. By hand:

```bash
cd f1-setup-lab
python -m venv .venv && . .venv/bin/activate
pip install ../skeleton/dist/trialforge_core-*.whl   # the SDK wheel (build it first; the start script does this)
pip install -e ..                  # the root database package (trialforge-db)
pip install -e .
f1-setup-lab                       # or: python -m f1lab
```

Open <http://127.0.0.1:8001>. **All state lives in the repository's single root database** `data/trialforge.db` (set `TRIALFORGE_DB` to use
another file), shared with WATTWISE LAB. Historical sessions (laps, telemetry, track location, weather, race datasets) and import jobs are
tables in that file (`f1_*`). The only other directory is the OpenF1 answer cache, `data/openf1-cache/`. Restart the server and everything is
still there; an import interrupted by a restart is marked `INTERRUPTED` and can be retried. Interactive API docs: <http://127.0.0.1:8001/api/docs>.

Starting it once applies migration 9, which recreates the setup-engineering tables that migration 8 had cleared. Existing OpenF1
sessions and WATTWISE data are kept, and the database is backed up under `data/backups/` first (see the root README).

## The Historical F1 screens

| Screen | What you do |
|---|---|
| **Historical F1** (landing page) | Pick season, Grand Prix, session and drivers (searchable, one, several or the whole field), choose which datasets to import, follow progress, cancel or retry. The session opens when the import finishes. Also lists recent imports and imported sessions. |
| **Imported sessions** | Every stored session; open, re-analyse. |
| Session: **Overview** | Session facts, drivers and best laps, which data is available, and the *Import coverage* panel (per dataset: status, rows, downloads, cache hits, fetch time, plus estimated and missing values). |
| **Lap analysis** | Lap and sector times, compound, tyre life, speed trap, pit laps per driver. |
| **Telemetry** | Speed, throttle, brake on/off, RPM, gear and DRS state against estimated lap distance. |
| **Track map** | The car's x/y track location, coloured by speed. |
| **Compare** | Two drivers or laps on a common distance grid with a time delta. |
| **Weather & tyres** | Air/track temperature, humidity, wind, pressure, rainfall flag; stints per driver. |
| **Race & results** | Starting grid against final classification (DNF/DNS/DSQ), running-order chart, gaps to leader and car ahead, overtakes. |
| **Pit, flags & radio** | Tyre-stint chart, pit stops, race-control timeline (with the driver each message names), team-radio links. |

From the command line:
`python scripts/import_historical.py --year 2025 --event "Abu Dhabi" --session R --drivers VER HAM`
(`--no-race-data` skips the race datasets, `--refresh-cache` downloads everything again). Inspect what was stored with
`python scripts/inspect_historical_session.py openf1-2025-abu-dhabi-grand-prix-r`.

## Choosing a session

Seasons run from 2023 (OpenF1's first season) to the current one; when the newest season has no published Grand Prix yet, the form steps back
to the latest one that has data. The Grand Prix, session and driver lists are loaded from OpenF1, never typed in: changing the season reloads
the Grand Prix list, changing the Grand Prix reloads its sessions, and changing the session reloads that session's driver roster (number, code,
name, team). The default is the latest Grand Prix that has started and its race. A "car" is the driver's entry in that session (OpenF1
publishes no separate chassis). Lap times are always imported; telemetry, track location, weather and each race dataset (results, running
position, grid, tyres and pit stops, overtakes and intervals, race control, team radio) can be switched off.

## Which OpenF1 endpoints are used

All 18 documented endpoints are integrated except the per-lap mini-sector arrays (`segments_sector_*`) and the driver headshots/team colours.

| OpenF1 endpoint | Stored as | Shown in |
|---|---|---|
| `meetings`, `sessions`, `drivers` | event list, session list, driver list | import form, overview |
| `laps` | lap table incl. sector times and `i1_speed_kph`, `i2_speed_kph`, `st_speed_kph` | Lap analysis |
| `stints`, `pit` | compound, stint, tyre life; pit stops (manifest) | Lap analysis, Pit, flags & radio |
| `car_data` | per-driver telemetry (speed, throttle, brake on/off, gear, rpm, raw DRS code) | Telemetry, Compare |
| `location` | per-driver x/y/z track location | Track map |
| `weather` | air/track temperature, humidity, wind, pressure, `rainfall_flag` | Weather & tyres |
| `race_control` | flags, safety car, messages (manifest) | Pit, flags & radio |
| `position` | **running order** over time (`race_position` dataset) | Race & results |
| `session_result` | classification, DNF/DNS/DSQ, points, times | Race & results |
| `starting_grid` | grid (filed by OpenF1 under the *qualifying* session; the importer looks it up there) | Race & results |
| `intervals`, `overtakes` | gaps to leader and car ahead; overtake events (race and sprint only) | Race & results |
| `team_radio` | clip links (https only; the audio stays on Formula 1's server) | Pit, flags & radio |
| `drivers_championship`, `teams_championship` | standings (beta endpoints, race sessions only); API and CLI only | none |

`location` (where the car is on the circuit) and `position` (P1, P2, ...) are different datasets and are never mixed: the UI says *track location*
for the first and *running order* for the second. Per-session datasets live in `f1_hist_dataset` and are served by
`GET /api/historical/sessions/{id}/datasets/{name}` (`race_position`, `session_result`, `starting_grid`, `intervals`, `overtakes`, `team_radio`,
`drivers_championship`, `teams_championship`, `pit_stops`, `race_control`; optional `driver` and `limit`).

## What the real API turned out to do (checked against api.openf1.org, November 2026)

* The starting grid is not under the race's session key. It is under the qualifying-type session (Qualifying for a race, Sprint Qualifying or the
  2023 "Sprint Shootout" for a sprint).
* Both championship endpoints returned no rows for every race and sprint tried (2023 to 2026). They are imported and reported as `empty` with a
  "beta endpoint" note, never filled in.
* A driver who retires has `position: null` with `dnf: true`; the UI shows the status, not a blank. Qualifying results carry one time per phase.
* Race-control messages often leave `driver_number` empty although the text names the car ("CAR 44 (HAM) LAP DELETED ..."); the driver is then
  taken from the message and marked as such.
* The 2023 Bahrain Grand Prix has no `pit` rows although its stints show tyre changes. Pit stops are not reconstructed from stints.
* `stop_duration` is only published from the 2024 United States GP onwards. Lapped drivers have a text gap such as `+1 LAP`, not seconds.
* Team radio is a limited selection of clips, without transcripts, and is thinner in 2026.

## Throttling, cache and refresh

* Requests are limited to 3 per second and 30 per minute, retried with back-off on HTTP 429 and 5xx (honouring `Retry-After`), and can be cancelled
  between requests. OpenF1 publishes no quota; the limits are deliberately conservative, so a full 20-driver race with car data and location
  (30-minute windows) takes several minutes the first time. Switch off datasets, telemetry or location in the form to import less.
* Every non-empty answer is cached in `data/openf1-cache/` with the time it was fetched. Answers expire after **30 days** (`OPENF1_CACHE_TTL_DAYS`;
  `0` disables the cache). A session that finished within the last 48 hours may still be gaining data, so its cached answers are only trusted for
  an hour and the import warns you. Empty answers are never cached.
* **Refresh:** tick *download again, ignoring the OpenF1 cache* (API field `refresh_cache`, CLI `--refresh-cache`) to re-download one session.
  *Clear cache* in the form (or `DELETE /api/historical/openf1/cache`) removes every cached answer; stored sessions are kept. A normal
  re-import of a stored session reuses the cache and sends no request for data it already holds.

## Partial imports

Laps are required. Everything else is optional. `partial: true` means an endpoint failed or a driver is missing car or location data. An empty
beta endpoint, or one that does not apply to the session type (grid, gaps and overtakes for practice or qualifying), is reported but is not a failure.

## Estimated and missing values (also listed in each manifest)

* **Estimated here:** `distance_m` is speed integrated over time within each lap (OpenF1 has no distance channel) and lap numbers of samples come
  from OpenF1's lap start times, which OpenF1 calls approximate. `session_time_s` counts from the session start OpenF1 publishes. `tyre_life` is
  tyre age at stint start plus laps in the stint. `round` is the meeting's place in the season order.
* **Raw, not converted:** x/y/z are OpenF1 location values in its own units with an arbitrary origin (not verified as metres, although the stored
  columns keep the canonical names `x_m`, `y_m`, `z_m`). DRS is the raw state code: 0 and 1 off, 8 detected, 10, 12 and 14 on, 2, 3 and 9 undocumented.
  `rainfall` is an on/off flag (`rainfall_flag`), not millimetres.
* **Not provided:** steering, corner positions, brake pressure, and lap validity. The UI shows lap validity as "not provided", never as zero invalid laps.

## Tests

```bash
cd f1-setup-lab && ../.venv/bin/python -m pytest -q
```

* `tests/test_openf1.py`: client (rate limiter, retries, cache expiry and refresh), importer and API against a fake server (`tests/openf1_fakes.py`).
* `tests/test_openf1_fixtures.py`: every endpoint's field names and the normalizers against **real downloaded responses** in
  `tests/fixtures/openf1/` (2023 race, 2024 qualifying, 2024 sprint, 2025 race, 2026 race; refresh them with `scripts/refresh_openf1_fixtures.py`).
* `tests/test_historical_repository.py`: storage round trips, atomic saves, cascading deletes, a full-size session.
* `tests/test_ui_openf1.py`: the browser flow (Chromium via Playwright) through the import form and every analysis tab.
* `tests/test_frontend_build.py`: an open tab reloads itself after the frontend files change.
* Opt-in real-network suite: `RUN_NETWORK_TESTS=1 pytest tests/test_openf1.py -k live` imports one driver from each of the five sessions above
  (several minutes because of the throttle). The default test run never contacts OpenF1.

## Limitations

Coverage is what OpenF1 publishes: 2023 onwards, approximate lap start times, no steering, corner positions, brake pressure or lap validity, and
championship endpoints that are currently empty. One import runs at a time. SQLite only; no authentication; single-process server.

**Attribution.** Data from OpenF1, an unofficial open-source API, not affiliated with Formula 1. The data belongs to its owners.
