#!/bin/bash
# Start / stop both applications together: F1 SETUP LAB (8001) and WATTWISE LAB (8002).
# Usage: scripts/start-all.sh [start|stop|status|check]      (start = default; Ctrl+C stops both)
# Env:   F1_PORT, WATTWISE_PORT, PYTHON_BIN, TRIALFORGE_DB (see scripts/start-f1.sh and scripts/start-wattwise.sh)
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
F1="$SCRIPT_DIR/start-f1.sh"
WW="$SCRIPT_DIR/start-wattwise.sh"
cmd="${1:-start}"

case "$cmd" in
    stop)   "$F1" stop; "$WW" stop; exit 0 ;;
    status) rc=0; "$F1" status || rc=1; "$WW" status || rc=1; exit $rc ;;
    check)  "$F1" check && "$WW" check; exit $? ;;
    start)  ;;
    *) echo "error: unknown command '$cmd' (use start, stop, status or check)" >&2; exit 1 ;;
esac

# refuse to start only one of them: check both first (interpreter, port free, not already running)
for app in "$F1" "$WW"; do
    if ! out="$("$app" check 2>&1)"; then
        echo "$out" >&2
        port="$(echo "$out" | sed -n 's/.*port \([0-9][0-9]*\) is already in use.*/\1/p')"
        if [[ -n "$port" ]] && command -v lsof >/dev/null 2>&1; then
            echo "Process(es) listening on port $port:" >&2
            lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | awk 'NR>1 {print "  pid " $2 " (" $1 ")"}' >&2
            echo "Stop it (kill <pid>) or use other ports: F1_PORT=18001 WATTWISE_PORT=18002 pnpm run start" >&2
        fi
        exit 1
    fi
done

echo "Starting F1 SETUP LAB (http://127.0.0.1:${F1_PORT:-8001}) and WATTWISE LAB (http://127.0.0.1:${WATTWISE_PORT:-8002}) ..."
echo "The first start installs dependencies and can take a few minutes. Press Ctrl+C to stop both."

pids=()
stopping=0
shutdown() {
    [[ $stopping -eq 1 ]] && return
    stopping=1
    echo
    echo "Stopping both applications ..."
    "$F1" stop >/dev/null 2>&1
    "$WW" stop >/dev/null 2>&1
    for p in "${pids[@]+"${pids[@]}"}"; do kill -TERM "$p" 2>/dev/null; done
}
trap shutdown INT TERM

"$F1" start </dev/null 2>&1 | sed -l 's/^/[f1]       /' &
pids+=($!)
"$WW" start </dev/null 2>&1 | sed -l 's/^/[wattwise] /' &
pids+=($!)

# if either application exits (crash, port taken after the check), stop the other one too
while kill -0 "${pids[0]}" 2>/dev/null && kill -0 "${pids[1]}" 2>/dev/null; do
    sleep 1
done
shutdown
wait 2>/dev/null
echo "Both applications stopped."
