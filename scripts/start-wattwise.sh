#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="$ROOT/wattwise-lab"
VENV="$APP/.venv"
WHEEL="$ROOT/skeleton/dist/trialforge_core-0.2.1-py3-none-any.whl"

PORT="${WATTWISE_PORT:-8002}"
PYTHON_BIN="${PYTHON_BIN:-python3.12}"

# Install dependencies on first launch
if [[ ! -x "$VENV/bin/wattwise-lab" ]]; then
    echo "Setting up WATTWISE LAB..."

    "$PYTHON_BIN" -m venv "$VENV"
    "$VENV/bin/python" -m pip install --upgrade pip
    "$VENV/bin/python" -m pip install "$WHEEL"
    "$VENV/bin/python" -m pip install -e "$APP"
fi

mkdir -p "$APP/data"
cd "$APP"

echo "Starting WATTWISE LAB on port $PORT"

exec "$VENV/bin/wattwise-lab" \
    --host 127.0.0.1 \
    --port "$PORT" \
    --db "$APP/data/wattwise.db"
