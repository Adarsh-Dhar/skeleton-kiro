#!/bin/bash
# Start / stop F1 SETUP LAB (historical F1 data from OpenF1 plus the setup-engineering lab built on the TRIALFORGE SDK; default port 8001, override with F1_PORT). The SDK wheel is built if missing and installed before the Backend starts; a missing wheel or failed install exits 1.
# Usage: scripts/start-f1.sh [start|stop|status|check]
# Env:   PYTHON_BIN (interpreter, must be >= 3.11), F1_PORT, TRIALFORGE_DB (default: <repo>/data/trialforge.db), HEALTH_TIMEOUT (seconds to wait for /api/health, default 30)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

run_app "f1" "F1 SETUP LAB" "$ROOT/f1-setup-lab" F1_PORT 8001 f1-setup-lab "${1:-start}" yes
