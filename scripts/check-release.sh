#!/bin/bash
# Release verification for the TRIALFORGE workspace, reproducible from a clean checkout.
#
# It builds its own virtual environment (default .run/release-venv, recreated on every run), builds the TRIALFORGE wheel from
# skeleton/packages/trialforge-core, installs that wheel (with its test extras), the root database package and both
# applications with their test dependencies, then runs every check. Each check is reported as PASSED, FAILED, SKIPPED
# (deliberately not run: opt-in network tests, real-game and real-household gates) or UNAVAILABLE (a tool is missing).
# Exit 1 if anything FAILED. Skipped and unavailable checks are never passes.
#
# Usage:  scripts/check-release.sh [--reuse]      --reuse keeps an existing release venv (faster, not a clean run)
# Env:    PYTHON_BIN (interpreter >= 3.11 used to create the venv), RELEASE_VENV (venv path)
# Opt-in: RUN_NETWORK_TESTS=1 (real FastF1 download; needs F1_USERNAME/F1_PASSWORD), RUN_START_SMOKE=1 (start both apps with
#         scripts/start-*.sh on F1_PORT/WATTWISE_PORT, default 8001/8002, against a throw-away database; occupied ports are
#         reported UNAVAILABLE and never touched; STARTUP_TIMEOUT seconds per app, default 900), INSTALL_CHROMIUM=1 (download Playwright's Chromium if missing; otherwise browser tests are UNAVAILABLE)
# Needs network access to PyPI to install dependencies.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=lib/common.sh
source "$ROOT/scripts/lib/common.sh"   # find_python (PYTHON_BIN, >= 3.11)
REUSE=0
[[ "${1:-}" == "--reuse" ]] && REUSE=1
VENV="${RELEASE_VENV:-$ROOT/.run/release-venv}"
PY="$VENV/bin/python"
LOG="$(mktemp -d)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
PASSED=() FAILED=() SKIPPED=() UNAVAILABLE=()

run() {  # run NAME CMD...
    local name="$1"; shift
    local f="$LOG/$(echo "$name" | tr -c 'a-zA-Z0-9' '_').log"
    printf '%-58s ' "$name"
    if "$@" >"$f" 2>&1; then
        PASSED+=("$name"); echo "PASSED"
    else
        FAILED+=("$name"); echo "FAILED  (log: $f)"; tail -5 "$f" | sed 's/^/    /'
    fi
}
skip() { printf '%-58s SKIPPED (%s)\n' "$1" "$2"; SKIPPED+=("$1: $2"); }
unavailable() { printf '%-58s UNAVAILABLE (%s)\n' "$1" "$2"; UNAVAILABLE+=("$1: $2"); }
has_py_mod() { "$PY" -c "import $1" >/dev/null 2>&1; }

# pytest exit 5 = nothing collected; surface pytest's own skips in the summary
pytest_run() {  # pytest_run NAME DIR ARGS...
    local name="$1" dir="$2"; shift 2
    local f="$LOG/$(echo "$name" | tr -c 'a-zA-Z0-9' '_').log"
    printf '%-58s ' "$name"
    (cd "$dir" && "$PY" -m pytest -q -p no:cacheprovider -rs "$@" </dev/null) >"$f" 2>&1
    local rc=$?
    local summary; summary="$(grep -E '^[0-9]+ (passed|failed)|passed|failed' "$f" | tail -1)"
    if [[ $rc -eq 0 ]]; then PASSED+=("$name"); echo "PASSED  ($summary)"; else FAILED+=("$name"); echo "FAILED  ($summary; log: $f)"; fi
    while IFS= read -r line; do SKIPPED+=("$name: ${line#SKIPPED }"); done < <(grep -E '^SKIPPED' "$f")
}

summary_and_exit() {
    echo
    echo "PASSED      ${#PASSED[@]}"
    echo "FAILED      ${#FAILED[@]}";      for x in "${FAILED[@]+"${FAILED[@]}"}"; do echo "  - $x"; done
    echo "SKIPPED     ${#SKIPPED[@]}";     for x in "${SKIPPED[@]+"${SKIPPED[@]}"}"; do echo "  - $x"; done
    echo "UNAVAILABLE ${#UNAVAILABLE[@]}"; for x in "${UNAVAILABLE[@]+"${UNAVAILABLE[@]}"}"; do echo "  - $x"; done
    echo "environment: $VENV"
    echo "logs: $LOG"
    echo "Skipped and unavailable checks are NOT passes."
    [[ ${#FAILED[@]} -eq 0 ]]
    exit $?
}

# ---------------------------------------------------------------- startup smoke (RUN_START_SMOKE=1)
# Starts both apps with the official start scripts (each installs into its own app .venv), against a throw-away
# database, on F1_PORT/WATTWISE_PORT (default 8001/8002). Never touches a process it did not start: an occupied port
# is reported as UNAVAILABLE. The EXIT trap stops anything this check started, on success and on failure.
SMOKE_F1_PORT="${F1_PORT:-8001}"
SMOKE_WW_PORT="${WATTWISE_PORT:-8002}"
SMOKE_STARTED=()          # "name|wrapper-pid" of apps this check started
SMOKE_TIMEOUT="${STARTUP_TIMEOUT:-900}"   # seconds per app (first start installs dependencies)

smoke_stop() {  # smoke_stop NAME  -- stop one app this check started
    local entry name pid
    for entry in "${SMOKE_STARTED[@]+"${SMOKE_STARTED[@]}"}"; do
        name="${entry%%|*}"; pid="${entry##*|}"
        [[ "$name" == "$1" ]] || continue
        "$ROOT/scripts/start-$name.sh" stop >/dev/null 2>&1
        local i; for i in $(seq 1 40); do kill -0 "$pid" 2>/dev/null || break; sleep 0.25; done
        kill -TERM "$pid" 2>/dev/null; sleep 0.5; kill -KILL "$pid" 2>/dev/null
        wait "$pid" 2>/dev/null
    done
    local keep=() e
    for e in "${SMOKE_STARTED[@]+"${SMOKE_STARTED[@]}"}"; do [[ "${e%%|*}" == "$1" ]] || keep+=("$e"); done
    SMOKE_STARTED=("${keep[@]+"${keep[@]}"}")
}
smoke_cleanup() { smoke_stop f1; smoke_stop wattwise; }

health_ok() {  # health_ok PORT APP TIMEOUT  -- poll /api/health until HTTP 200 with the expected app and SDK version
    "$PY" - "$1" "$2" "$3" "$SDK_VERSION" <<'EOF'
import json, sys, time, urllib.request
port, app, timeout, sdk = int(sys.argv[1]), sys.argv[2], float(sys.argv[3]), sys.argv[4]
end, last = time.time() + timeout, "no response"
while time.time() < end:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=3) as r:
            body = json.loads(r.read())
            if r.status != 200:
                last = f"HTTP {r.status}"
            elif body.get("app") != app:
                sys.exit(f"port {port} answers as {body.get('app')!r}, not {app!r}")
            elif body.get("trialforge") != sdk:
                sys.exit(f"{app} runs trialforge {body.get('trialforge')}, expected {sdk}")
            else:
                print(f"HTTP 200, {app}, trialforge {sdk}")
                print(f"trialforge loaded from {body.get('trialforge_path')}", file=sys.stderr)
                sys.exit(0)
    except OSError as exc:
        last = str(exc)
    time.sleep(1)
sys.exit(f"no healthy response on port {port} within {timeout:.0f}s ({last})")
EOF
}

smoke_start() {  # smoke_start NAME PORT_VAR PORT APP_NAME
    local name="$1" pvar="$2" port="$3" app="$4" log="$LOG/start-$1.log"
    printf '%-58s ' "startup smoke: start $app on port $port"
    if ! port_free "$PY" "$port"; then
        UNAVAILABLE+=("startup smoke $app: port $port is already in use by another process (not touched); free it or set $pvar")
        echo "UNAVAILABLE (port $port already in use; not touched)"; return 1
    fi
    if pid_alive "$(read_pid "$name")"; then
        UNAVAILABLE+=("startup smoke $app: an instance from this checkout is already running (pid file .run/$name.pid)")
        echo "UNAVAILABLE (already running from this checkout)"; return 1
    fi
    env "$pvar=$port" TRIALFORGE_DB="$SMOKE_DB" "$ROOT/scripts/start-$name.sh" start </dev/null >"$log" 2>&1 &
    SMOKE_STARTED+=("$name|$!")
    if health_ok "$port" "$app" "$SMOKE_TIMEOUT" >>"$log" 2>&1; then
        local spid; spid="$(read_pid "$name")"
        if ! pid_alive "$spid"; then
            FAILED+=("startup smoke: $app (no live server pid in .run/$name.pid)"); echo "FAILED  (no server pid; log: $log)"; return 1
        fi
        if command -v lsof >/dev/null 2>&1 && ! lsof -nP -a -p "$spid" -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
            FAILED+=("startup smoke: $app (port $port is not served by pid $spid)"); echo "FAILED  (port not owned by the started server; log: $log)"; return 1
        fi
        PASSED+=("startup smoke: start $app on port $port"); echo "PASSED  (pid $spid, $(grep '^HTTP 200' "$log" | tail -1))"
        return 0
    fi
    FAILED+=("startup smoke: start $app on port $port"); echo "FAILED  (log: $log)"; tail -5 "$log" | sed 's/^/    /'
    return 1
}

both_healthy() { health_ok "$SMOKE_F1_PORT" f1-setup-lab 10 && health_ok "$SMOKE_WW_PORT" wattwise-lab 10; }

start_smoke() {
    SMOKE_DB="$TMP/startup-smoke/trialforge.db"
    mkdir -p "$(dirname "$SMOKE_DB")"
    local f1_ok=0 ww_ok=0
    smoke_start f1 F1_PORT "$SMOKE_F1_PORT" f1-setup-lab && f1_ok=1
    smoke_start wattwise WATTWISE_PORT "$SMOKE_WW_PORT" wattwise-lab && ww_ok=1
    if [[ $f1_ok -eq 1 && $ww_ok -eq 1 ]]; then
        run "startup smoke: both apps healthy at the same time" both_healthy
        run "startup smoke: shared database created and migrated" "$PY" - "$SMOKE_DB" <<'EOF'
import sys
from sqlalchemy import inspect
from db import create_db_engine, current_version, latest_version
e = create_db_engine(sys.argv[1])
assert current_version(e) == latest_version(), (current_version(e), latest_version())
tables = set(inspect(e).get_table_names())
missing = {"experiments", "app_experiment_owner", "f1_import_job", "ww_household", "ww_tariff"} - tables
assert not missing, f"missing tables: {missing}"
print("schema", current_version(e))
EOF
        # independence: stopping F1 must not affect WATTWISE
        smoke_stop f1
        run "startup smoke: WATTWISE keeps running after F1 stops" health_ok "$SMOKE_WW_PORT" wattwise-lab 10
        run "startup smoke: F1 port $SMOKE_F1_PORT released after stop" port_free "$PY" "$SMOKE_F1_PORT"
    fi
    smoke_cleanup
    if [[ $ww_ok -eq 1 ]]; then
        run "startup smoke: WATTWISE port $SMOKE_WW_PORT released after stop" port_free "$PY" "$SMOKE_WW_PORT"
    fi
}

trap 'smoke_cleanup; rm -rf "$TMP"' EXIT
trap 'echo; echo "interrupted"; exit 130' INT TERM

BASE_PY="$(find_python)" || exit 1
echo "TRIALFORGE release check  ($(date -u +%Y-%m-%dT%H:%M:%SZ))"
echo "base interpreter: $BASE_PY ($("$BASE_PY" --version 2>&1)); release environment: $VENV ($([[ $REUSE -eq 1 ]] && echo reused || echo fresh))"
echo

# ---------------------------------------------------------------- 1. dedicated release environment
SDK_VERSION="$(sdk_version)"
WHEEL="$DIST_DIR/trialforge_core-$SDK_VERSION-py3-none-any.whl"   # skeleton/dist: the wheel the start scripts install
if [[ $REUSE -eq 0 || ! -x "$PY" ]]; then
    rm -rf "$VENV"
    run "create release virtual environment" "$BASE_PY" -m venv "$VENV"
fi
run "install build tools (pip, build, ruff, packaging)" "$PY" -m pip install -q --upgrade pip build ruff packaging
rm -f "$WHEEL"   # always rebuilt from the current source
run "build SDK wheel trialforge-core $SDK_VERSION" "$PY" -m build --wheel --outdir "$DIST_DIR" skeleton/packages/trialforge-core
run "install SDK wheel with test extras" "$PY" -m pip install -q --force-reinstall "$WHEEL[test,verify]"
run "install root database package (trialforge-db)" "$PY" -m pip install -q -e .
run "install F1 SETUP LAB + test dependencies" "$PY" -m pip install -q -e "./f1-setup-lab[test,migrate]"
run "install WATTWISE LAB + test dependencies" "$PY" -m pip install -q -e "./wattwise-lab[test]"
if [[ ${#FAILED[@]} -gt 0 ]]; then
    echo "The release environment could not be built (network access to PyPI?); no further checks were run."
    summary_and_exit
fi

# ---------------------------------------------------------------- 2. other packages build
run "build root db package wheel" "$PY" -m pip wheel --no-deps -q -w "$TMP/dist" .
run "build f1-setup-lab wheel" "$PY" -m pip wheel --no-deps -q -w "$TMP/dist" ./f1-setup-lab
run "build wattwise-lab wheel" "$PY" -m pip wheel --no-deps -q -w "$TMP/dist" ./wattwise-lab

# ---------------------------------------------------------------- imports and SDK version
run "import both applications" "$PY" -c "import f1lab.api.app, wattwise.api.app, db"
run "both apps use the installed SDK $SDK_VERSION from site-packages" "$PY" - "$SDK_VERSION" "$VENV" <<'EOF'
import sys, importlib.metadata as md, json
from pathlib import Path
import trialforge
want, venv = sys.argv[1], Path(sys.argv[2]).resolve()
assert Path(sys.prefix).resolve() == venv, f"not running in the release environment: {sys.prefix}"
assert Path(trialforge.__file__).resolve().is_relative_to(venv), f"trialforge imported from {trialforge.__file__}, outside the release environment"
direct = md.distribution("trialforge-core").read_text("direct_url.json")
assert not (direct and json.loads(direct).get("dir_info", {}).get("editable")), "trialforge-core is an editable install, not the wheel"
import f1lab.api.app, wattwise.api.app  # noqa: E401
for mod in (f1lab, wattwise):
    assert not hasattr(mod, "ExperimentService"), f"{mod.__name__} re-exports SDK classes"
assert trialforge.__version__ == want, f"installed trialforge {trialforge.__version__} != repository SDK {want}"
assert "site-packages" in Path(trialforge.__file__).as_posix(), f"trialforge imported from {trialforge.__file__}, not the installed package"
from packaging.requirements import Requirement
for app in ("f1-setup-lab", "wattwise-lab"):
    reqs = [Requirement(r) for r in (md.requires(app) or []) if r.startswith("trialforge-core")]
    assert reqs and reqs[0].specifier.contains(want), f"{app} does not accept trialforge-core {want}: {reqs}"
print("ok", want)
EOF

# ---------------------------------------------------------------- static checks
run "lint: root (db, scripts, tests)" "$PY" -m ruff check .
run "lint: f1-setup-lab" bash -c "cd f1-setup-lab && '$PY' -m ruff check ."
run "lint: wattwise-lab" bash -c "cd wattwise-lab && '$PY' -m ruff check ."
run "lint: SDK (skeleton)" bash -c "cd skeleton && '$PY' -m ruff check ."
run "compile all Python files" "$PY" -m compileall -q db scripts tests f1-setup-lab/backend f1-setup-lab/scripts f1-setup-lab/tests \
    wattwise-lab/backend wattwise-lab/tests skeleton/packages/trialforge-core/src
if command -v node >/dev/null 2>&1; then
    run "JavaScript syntax (both frontends)" bash -c 'for f in f1-setup-lab/frontend/*.js wattwise-lab/frontend/*.js; do node --check "$f" || exit 1; done'
else
    unavailable "JavaScript syntax" "node is not installed"
fi
run "startup scripts: bash syntax" bash -c 'bash -n scripts/start-f1.sh && bash -n scripts/start-wattwise.sh && bash -n scripts/lib/common.sh'

# ---------------------------------------------------------------- tests
pytest_run "SDK tests (trialforge-core)" skeleton/packages/trialforge-core
pytest_run "SDK workspace tests (skeleton/tests)" skeleton tests
pytest_run "root tests (db, scoping, start scripts, migration)" . tests
pytest_run "F1 SETUP LAB backend tests" f1-setup-lab --ignore=tests/test_ui_browser.py
pytest_run "WATTWISE LAB backend tests" wattwise-lab --ignore=tests/test_ui_browser.py
chromium_ok() { "$PY" -c "from playwright.sync_api import sync_playwright as s
with s() as p: p.chromium.launch().close()" >/dev/null 2>&1; }
if ! chromium_ok && [[ -n "${INSTALL_CHROMIUM:-}" ]]; then
    run "install Playwright Chromium" "$PY" -m playwright install chromium
fi
if chromium_ok; then
    pytest_run "F1 SETUP LAB browser tests (Chromium)" f1-setup-lab tests/test_ui_browser.py
    pytest_run "WATTWISE LAB browser tests (Chromium)" wattwise-lab tests/test_ui_browser.py
else
    unavailable "browser tests (both apps)" "Chromium for this Playwright version is not installed; rerun with INSTALL_CHROMIUM=1"
fi
if [[ -n "${RUN_NETWORK_TESTS:-}" ]]; then
    printf '%-58s ' "real FastF1 import (smoke_fastf1_import.py)"
    (cd f1-setup-lab && "$PY" scripts/smoke_fastf1_import.py --db "$TMP/smoke.db" </dev/null) >"$LOG/smoke.log" 2>&1
    case $? in
        0) PASSED+=("real FastF1 import"); echo "PASSED" ;;
        3) SKIPPED+=("real FastF1 import: $(tail -1 "$LOG/smoke.log")"); echo "SKIPPED ($(tail -1 "$LOG/smoke.log"))" ;;
        *) FAILED+=("real FastF1 import"); echo "FAILED  (log: $LOG/smoke.log)" ;;
    esac
else
    skip "real FastF1 import (smoke_fastf1_import.py)" "opt-in: RUN_NETWORK_TESTS=1; needs F1_USERNAME/F1_PASSWORD"
fi
if [[ -n "${RUN_START_SMOKE:-}" ]]; then
    start_smoke
else
    skip "startup smoke: both apps via start scripts on 8001/8002" "opt-in: RUN_START_SMOKE=1"
fi
skip "real EA SPORTS F1 25 UDP session and setup-mapping probe" "manual gate: needs the game (scripts/probe_setup.py --guided)"
skip "real household meter export, tariff and bill reconciliation" "manual gate: needs your data"

summary_and_exit
