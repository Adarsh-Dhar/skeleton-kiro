#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="$ROOT/f1-setup-lab"
VENV="$APP/.venv"
WHEEL="$ROOT/skeleton/dist/trialforge_core-0.2.1-py3-none-any.whl"

PORT="${F1_PORT:-8001}"
PYTHON_BIN="${PYTHON_BIN:-python3.12}"

# Install dependencies on first launch
if [[ ! -x "$VENV/bin/f1-setup-lab" ]]; then
    echo "Setting up F1 SETUP LAB..."

    "$PYTHON_BIN" -m venv "$VENV"
    "$VENV/bin/python" -m pip install --upgrade pip
    "$VENV/bin/python" -m pip install "$WHEEL"
    "$VENV/bin/python" -m pip install -e "$APP"
fi

mkdir -p "$APP/data"
cd "$APP"

echo "Starting F1 SETUP LAB on port $PORT"

exec "$VENV/bin/f1-setup-lab" \
    --host 127.0.0.1 \
    --port "$PORT" \
    --db "$APP/data/f1lab.db"
