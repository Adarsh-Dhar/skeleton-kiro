#!/bin/bash
# Release verification for the TRIALFORGE workspace. Runs every check and reports each as PASSED, FAILED, SKIPPED
# (deliberately not run, e.g. opt-in network tests) or UNAVAILABLE (a tool is missing). Exit 1 if anything FAILED.
#
# Usage: scripts/check-release.sh            Env: PYTHON (default: .venv/bin/python, else python3)
# Opt-in: RUN_NETWORK_TESTS=1 (real FastF1 download), RUN_START_SMOKE=1 (full install + start of both apps)
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="${PYTHON:-}"
if [[ -z "$PY" ]]; then
    if [[ -x "$ROOT/.venv/bin/python" ]]; then PY="$ROOT/.venv/bin/python"; else PY="$(command -v python3)"; fi
fi
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

echo "TRIALFORGE release check  ($(date -u +%Y-%m-%dT%H:%M:%SZ))   python: $PY ($("$PY" --version 2>&1))"
echo

# ---------------------------------------------------------------- packages
if has_py_mod build; then
    run "build SDK wheel (trialforge-core)" "$PY" -m build --wheel --outdir "$TMP/dist" skeleton/packages/trialforge-core
else
    run "build SDK wheel (trialforge-core, pip wheel)" "$PY" -m pip wheel --no-deps -q -w "$TMP/dist" skeleton/packages/trialforge-core
fi
run "build root db package (trialforge-db)" "$PY" -m pip wheel --no-deps -q -w "$TMP/dist" .
run "build f1-setup-lab package" "$PY" -m pip wheel --no-deps -q -w "$TMP/dist" ./f1-setup-lab
run "build wattwise-lab package" "$PY" -m pip wheel --no-deps -q -w "$TMP/dist" ./wattwise-lab

# ---------------------------------------------------------------- imports and SDK version
run "import both applications" "$PY" -c "import f1lab.api.app, wattwise.api.app, db"
SDK_VERSION="$(sed -n 's/^version *= *"\(.*\)"/\1/p' skeleton/packages/trialforge-core/pyproject.toml | head -1)"
run "both apps use the installed public SDK $SDK_VERSION" "$PY" - "$SDK_VERSION" <<'EOF'
import sys, importlib.metadata as md
from pathlib import Path
import trialforge
want = sys.argv[1]
assert trialforge.__version__ == want, f"installed trialforge {trialforge.__version__} != repository SDK {want}"
assert "site-packages" in Path(trialforge.__file__).as_posix(), f"trialforge imported from {trialforge.__file__}, not the installed package"
from packaging.requirements import Requirement
for app in ("f1-setup-lab", "wattwise-lab"):
    reqs = [Requirement(r) for r in (md.requires(app) or []) if r.startswith("trialforge-core")]
    assert reqs and reqs[0].specifier.contains(want), f"{app} does not accept trialforge-core {want}: {reqs}"
print("ok", want)
EOF

# ---------------------------------------------------------------- static checks
if has_py_mod ruff || command -v ruff >/dev/null 2>&1; then
    RUFF=("$PY" -m ruff); has_py_mod ruff || RUFF=(ruff)
    run "lint: root (db, scripts, tests)" "${RUFF[@]}" check .
    run "lint: f1-setup-lab" bash -c "cd f1-setup-lab && ${RUFF[*]} check ."
    run "lint: wattwise-lab" bash -c "cd wattwise-lab && ${RUFF[*]} check ."
    run "lint: SDK (skeleton)" bash -c "cd skeleton && ${RUFF[*]} check ."
else
    unavailable "lint" "ruff is not installed (pip install ruff)"
fi
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
if has_py_mod playwright && "$PY" -c "from playwright.sync_api import sync_playwright as s
with s() as p: p.chromium.launch().close()" >/dev/null 2>&1; then
    pytest_run "F1 SETUP LAB browser tests (Chromium)" f1-setup-lab tests/test_ui_browser.py
    pytest_run "WATTWISE LAB browser tests (Chromium)" wattwise-lab tests/test_ui_browser.py
else
    unavailable "browser tests" "Playwright/Chromium not installed (pip install playwright && python -m playwright install chromium)"
fi
[[ -n "${RUN_NETWORK_TESTS:-}" ]] || skip "real FastF1 import (smoke_fastf1_import.py)" "opt-in: RUN_NETWORK_TESTS=1; needs F1_USERNAME/F1_PASSWORD"
[[ -n "${RUN_START_SMOKE:-}" ]] || skip "full install + start of both apps on 8001/8002" "opt-in: RUN_START_SMOKE=1"

# ---------------------------------------------------------------- summary
echo
echo "PASSED      ${#PASSED[@]}"
echo "FAILED      ${#FAILED[@]}";      for x in "${FAILED[@]+"${FAILED[@]}"}"; do echo "  - $x"; done
echo "SKIPPED     ${#SKIPPED[@]}";     for x in "${SKIPPED[@]+"${SKIPPED[@]}"}"; do echo "  - $x"; done
echo "UNAVAILABLE ${#UNAVAILABLE[@]}"; for x in "${UNAVAILABLE[@]+"${UNAVAILABLE[@]}"}"; do echo "  - $x"; done
echo "logs: $LOG"
echo "Skipped and unavailable checks are NOT passes."
[[ ${#FAILED[@]} -eq 0 ]]
