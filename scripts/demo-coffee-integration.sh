#!/bin/bash
# Run the third-domain (coffee roasting) demonstration against the installed TRIALFORGE SDK. All coffee data is SYNTHETIC.
# Usage: scripts/demo-coffee-integration.sh      Env: PYTHON_BIN, DEMO_VENV (default .run/demo-venv, created on first use)
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/lib/common.sh"
VENV="${DEMO_VENV:-$ROOT/.run/demo-venv}"
if [[ ! -x "$VENV/bin/python" ]]; then
    PY0="$(find_python)"
    "$PY0" -m venv "$VENV"
    "$VENV/bin/python" -m pip install -q --upgrade pip
    "$VENV/bin/python" -m pip install -q "$ROOT/skeleton/packages/trialforge-core"
fi
echo "Coffee-roasting integration demo (synthetic data) using trialforge from: $("$VENV/bin/python" -c 'import trialforge;print(trialforge.__file__)')"
cd "$ROOT/skeleton"
"$VENV/bin/python" -m examples.third_domain_adapter.run_example
