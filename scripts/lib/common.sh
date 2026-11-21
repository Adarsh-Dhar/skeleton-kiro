#!/bin/bash
# Shared helpers for start-f1.sh / start-wattwise.sh. Source this file; do not execute it.
# Works with the bash 3.2 that ships with macOS.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SDK_DIR="$ROOT/skeleton/packages/trialforge-core"
DIST_DIR="${TRIALFORGE_DIST_DIR:-$ROOT/skeleton/dist}"
RUN_DIR="$ROOT/.run"
MIN_PY_MAJOR=3
MIN_PY_MINOR=11

die() { echo "error: $*" >&2; exit 1; }
info() { echo "$*"; }

# ---------------------------------------------------------------- python
py_ok() { "$1" -c "import sys; sys.exit(0 if sys.version_info >= ($MIN_PY_MAJOR, $MIN_PY_MINOR) else 1)" >/dev/null 2>&1; }

# Prints the interpreter to use. PYTHON_BIN wins (and must be >= 3.11); otherwise the newest of 3.13/3.12/3.11/python3.
find_python() {
    if [[ -n "${PYTHON_BIN:-}" ]]; then
        command -v "$PYTHON_BIN" >/dev/null 2>&1 || die "PYTHON_BIN='$PYTHON_BIN' was not found"
        py_ok "$PYTHON_BIN" || die "PYTHON_BIN='$PYTHON_BIN' is older than Python $MIN_PY_MAJOR.$MIN_PY_MINOR"
        command -v "$PYTHON_BIN"
        return
    fi
    local cand
    for cand in python3.13 python3.12 python3.11 python3; do
        if command -v "$cand" >/dev/null 2>&1 && py_ok "$cand"; then
            command -v "$cand"
            return
        fi
    done
    die "no Python >= $MIN_PY_MAJOR.$MIN_PY_MINOR found; install one or set PYTHON_BIN"
}

# ---------------------------------------------------------------- SDK wheel
sdk_version() {
    sed -n 's/^version *= *"\(.*\)"/\1/p' "$SDK_DIR/pyproject.toml" | head -1
}

wheel_path() { echo "$DIST_DIR/trialforge_core-$(sdk_version)-py3-none-any.whl"; }

# Builds the SDK wheel from the local package when it is missing (the wheel is not committed).
ensure_wheel() {
    local py="$1" wheel
    wheel="$(wheel_path)"
    [[ -f "$wheel" ]] && return 0
    [[ -d "$SDK_DIR" ]] || die "SDK package not found at $SDK_DIR"
    mkdir -p "$DIST_DIR"
    # both start scripts may run at once: only one builds, the other waits for the finished wheel
    local lock="$DIST_DIR/.build.lock" waited=0
    until mkdir "$lock" 2>/dev/null; do
        [[ -f "$wheel" ]] && return 0
        sleep 1; waited=$((waited + 1))
        [[ $waited -lt 600 ]] || die "timed out waiting for another process to build the SDK wheel (remove $lock if stale)"
    done
    if [[ -f "$wheel" ]]; then rmdir "$lock"; return 0; fi
    info "Building $(basename "$wheel") from $SDK_DIR ..."
    local err=""
    # build in a throw-away venv: system/Homebrew Pythons refuse global pip installs (PEP 668)
    local bvenv="$RUN_DIR/build-venv"
    mkdir -p "$RUN_DIR"
    if [[ ! -x "$bvenv/bin/python" ]] && ! "$py" -m venv "$bvenv" >/dev/null 2>&1; then
        err="could not create a build environment with $py"
    elif ! "$bvenv/bin/python" -m pip install --quiet build >/dev/null 2>&1; then
        err="could not install the 'build' package (offline?)"
    elif ! "$bvenv/bin/python" -m build --wheel --outdir "$DIST_DIR" "$SDK_DIR" >/dev/null 2>&1; then
        err="building the SDK wheel failed (run: $bvenv/bin/python -m build --wheel $SDK_DIR)"
    elif [[ ! -f "$wheel" ]]; then
        err="wheel build finished but $wheel does not exist"
    fi
    rmdir "$lock" 2>/dev/null || true
    [[ -z "$err" ]] || die "$err"
}

# ---------------------------------------------------------------- ports
port_free() {
    "$1" - "$2" <<'EOF'
import socket, sys
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(("127.0.0.1", int(sys.argv[1])))
except OSError:
    sys.exit(1)
finally:
    s.close()
EOF
}

# ---------------------------------------------------------------- pid files
pid_file() { echo "$RUN_DIR/$1.pid"; }

pid_alive() { [[ -n "${1:-}" ]] && kill -0 "$1" >/dev/null 2>&1; }

read_pid() { [[ -f "$(pid_file "$1")" ]] && cat "$(pid_file "$1")" || true; }

stop_app() {
    local name="$1" pid
    pid="$(read_pid "$name")"
    if ! pid_alive "$pid"; then
        rm -f "$(pid_file "$name")"
        info "$name is not running"
        return 0
    fi
    kill -TERM "$pid" 2>/dev/null || true
    local i
    for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
        pid_alive "$pid" || break
        sleep 0.25
    done
    pid_alive "$pid" && kill -KILL "$pid" 2>/dev/null || true
    rm -f "$(pid_file "$name")"
    info "$name stopped"
}

status_app() {
    local name="$1" pid
    pid="$(read_pid "$name")"
    if pid_alive "$pid"; then
        info "$name is running (pid $pid)"
    else
        info "$name is not running"
        return 1
    fi
}

# ---------------------------------------------------------------- install
# Creates or reuses the app venv and installs: SDK wheel, root db package, the app. A stamp file skips reinstalling.
install_app() {
    local py="$1" venv="$2" app_dir="$3" wheel stamp want
    wheel="$(wheel_path)"
    if [[ ! -x "$venv/bin/python" ]]; then
        info "Creating virtual environment $venv"
        "$py" -m venv "$venv" || die "could not create the virtual environment"
    fi
    py_ok "$venv/bin/python" || die "$venv uses Python older than $MIN_PY_MAJOR.$MIN_PY_MINOR; delete it and retry"
    stamp="$venv/.trialforge-install-stamp"
    want="$(basename "$wheel")|$(cksum < "$ROOT/pyproject.toml")|$(cksum < "$app_dir/pyproject.toml")"
    if [[ "$(cat "$stamp" 2>/dev/null || true)" != "$want" ]]; then
        info "Installing dependencies ..."
        "$venv/bin/python" -m pip install --quiet --upgrade pip || die "pip upgrade failed"
        "$venv/bin/python" -m pip install --quiet --force-reinstall "$wheel" || die "installing the SDK wheel failed"
        "$venv/bin/python" -m pip install --quiet -e "$ROOT" || die "installing the root db package failed"
        "$venv/bin/python" -m pip install --quiet -e "$app_dir" || die "installing the app failed"
        echo "$want" > "$stamp"
    fi
    local have
    have="$("$venv/bin/python" -c "import importlib.metadata as m; print(m.version('trialforge-core'))" 2>/dev/null || true)"
    [[ "$have" == "$(sdk_version)" ]] || die "installed trialforge-core is '$have' but the repository SDK is '$(sdk_version)'"
    info "trialforge-core $have OK"
}

# ---------------------------------------------------------------- main entry
# run_app NAME LABEL APP_DIR PORT_VAR DEFAULT_PORT ENTRYPOINT [start|stop|status|check]
run_app() {
    local name="$1" label="$2" app_dir="$3" port_var="$4" default_port="$5" entry="$6" cmd="${7:-start}"
    local port="${!port_var:-$default_port}"
    local venv="$app_dir/.venv"
    export TRIALFORGE_DB="${TRIALFORGE_DB:-$ROOT/data/trialforge.db}"

    case "$cmd" in
        stop) stop_app "$name"; return ;;
        status) status_app "$name"; return ;;
        start|check) ;;
        *) die "unknown command '$cmd' (use start, stop, status or check)" ;;
    esac

    local py
    py="$(find_python)"
    local existing
    existing="$(read_pid "$name")"
    if pid_alive "$existing"; then
        die "$label is already running (pid $existing). Stop it with: $0 stop"
    fi
    rm -f "$(pid_file "$name")"
    port_free "$py" "$port" || die "port $port is already in use (another process is listening). Set $port_var to use a different port."

    if [[ "$cmd" == "check" ]]; then
        local wheel_state="present"
        [[ -f "$(wheel_path)" ]] || wheel_state="missing (would be built)"
        info "python:  $py"
        info "wheel:   $(wheel_path) [$wheel_state]"
        info "venv:    $venv"
        info "port:    $port (free)"
        info "db:      $TRIALFORGE_DB"
        return 0
    fi

    ensure_wheel "$py"
    install_app "$py" "$venv" "$app_dir"
    mkdir -p "$(dirname "$TRIALFORGE_DB")" "$RUN_DIR"
    "$venv/bin/python" -m db.migrate --db "$TRIALFORGE_DB" >/dev/null || die "database migration failed"

    info "Starting $label on http://127.0.0.1:$port  (database: $TRIALFORGE_DB)"
    "$venv/bin/$entry" --host 127.0.0.1 --port "$port" &
    local child=$!
    echo "$child" > "$(pid_file "$name")"
    trap 'kill -TERM "$child" 2>/dev/null || true' INT TERM
    trap 'rm -f "$(pid_file "$name")"' EXIT
    local rc=0
    wait "$child" || rc=$?
    # first wait returns early when a trapped signal arrives; wait again so the server can shut down cleanly
    if pid_alive "$child"; then wait "$child" || rc=$?; fi
    return "$rc"
}
