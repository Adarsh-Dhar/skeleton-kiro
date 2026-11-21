#!/bin/bash
# Start / stop WATTWISE LAB (default port 8002, override with WATTWISE_PORT).
# Usage: scripts/start-wattwise.sh [start|stop|status|check]
# Env:   PYTHON_BIN (interpreter, must be >= 3.11), WATTWISE_PORT, TRIALFORGE_DB (default: <repo>/data/trialforge.db)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

run_app "wattwise" "WATTWISE LAB" "$ROOT/wattwise-lab" WATTWISE_PORT 8002 wattwise-lab "${1:-start}"
