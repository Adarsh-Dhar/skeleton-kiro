"""Task 3: start scripts. Fast checks use the ``check`` command (no installs); a full start is opt-in (RUN_START_SMOKE=1)."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = {"f1": ROOT / "scripts" / "start-f1.sh", "wattwise": ROOT / "scripts" / "start-wattwise.sh"}
PORT_VARS = {"f1": "F1_PORT", "wattwise": "WATTWISE_PORT"}


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def run(script: Path, *args: str, env: dict | None = None, timeout: int = 60) -> subprocess.CompletedProcess:
    e = {**os.environ, **(env or {})}
    return subprocess.run(["bash", str(script), *args], capture_output=True, text=True, env=e, timeout=timeout, cwd=ROOT)


@pytest.mark.parametrize("name", ["f1", "wattwise"])
def test_scripts_are_valid_bash_and_executable(name):
    assert os.access(SCRIPTS[name], os.X_OK)
    assert subprocess.run(["bash", "-n", str(SCRIPTS[name])]).returncode == 0
    assert subprocess.run(["bash", "-n", str(ROOT / "scripts" / "lib" / "common.sh")]).returncode == 0


@pytest.mark.parametrize("name", ["f1", "wattwise"])
def test_check_reports_python_wheel_venv_port_and_root_db(name, tmp_path):
    r = run(SCRIPTS[name], "check", env={PORT_VARS[name]: str(free_port()), "TRIALFORGE_DB": str(tmp_path / "t.db"), "TRIALFORGE_DIST_DIR": str(tmp_path / "dist")})
    assert r.returncode == 0, r.stderr
    assert "python:" in r.stdout and "venv:" in r.stdout
    assert "missing (would be built)" in r.stdout  # no wheel in the empty dist dir
    assert f"db:      {tmp_path / 't.db'}" in r.stdout


def test_default_database_is_the_root_database():
    r = run(SCRIPTS["f1"], "check", env={"F1_PORT": str(free_port()), "TRIALFORGE_DB": ""})
    assert r.returncode == 0, r.stderr
    assert f"db:      {ROOT / 'data' / 'trialforge.db'}" in r.stdout


@pytest.mark.parametrize("name", ["f1", "wattwise"])
def test_occupied_port_gives_a_clear_error(name):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    try:
        r = run(SCRIPTS[name], "check", env={PORT_VARS[name]: str(s.getsockname()[1])})
    finally:
        s.close()
    assert r.returncode != 0
    assert "already in use" in r.stderr and PORT_VARS[name] in r.stderr


def test_python_bin_override_is_validated(tmp_path):
    r = run(SCRIPTS["f1"], "check", env={"PYTHON_BIN": "definitely-not-a-python", "F1_PORT": str(free_port())})
    assert r.returncode != 0 and "was not found" in r.stderr
    old = tmp_path / "python-old"
    old.write_text("#!/bin/bash\nexit 1\n")
    old.chmod(0o755)
    r = run(SCRIPTS["f1"], "check", env={"PYTHON_BIN": str(old), "F1_PORT": str(free_port())})
    assert r.returncode != 0 and "older than Python 3.11" in r.stderr


def test_accepts_any_python_311_or_newer_by_default(tmp_path):
    """No hardcoded python3.12: with PYTHON_BIN unset the script picks any >=3.11 interpreter on PATH."""
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    (shim_dir / "python3").symlink_to(sys.executable)
    r = run(SCRIPTS["f1"], "check", env={"PATH": f"{shim_dir}:/usr/bin:/bin", "F1_PORT": str(free_port()), "PYTHON_BIN": ""})
    assert r.returncode == 0, r.stderr


def test_stop_and_status_when_not_running(tmp_path):
    for name in SCRIPTS:
        assert "not running" in run(SCRIPTS[name], "stop").stdout
        assert run(SCRIPTS[name], "status").returncode == 1


def test_unknown_command_is_rejected():
    r = run(SCRIPTS["f1"], "frobnicate")
    assert r.returncode != 0 and "unknown command" in r.stderr


@pytest.mark.skipif(not os.environ.get("RUN_START_SMOKE"), reason="full install+start smoke test is opt-in (RUN_START_SMOKE=1; needs network for pip)")
def test_both_apps_start_independently_on_one_database(tmp_path):
    db = tmp_path / "trialforge.db"
    ports = {"f1": free_port(), "wattwise": free_port()}
    procs = {}
    try:
        for name, script in SCRIPTS.items():
            procs[name] = subprocess.Popen(["bash", str(script)], env={**os.environ, PORT_VARS[name]: str(ports[name]), "TRIALFORGE_DB": str(db)},
                                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=ROOT)
        for name, port in ports.items():
            deadline = time.time() + 600
            while time.time() < deadline:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as resp:
                        assert resp.status == 200
                        break
                except OSError:
                    time.sleep(1)
            else:
                pytest.fail(f"{name} did not become healthy")
        assert db.exists()
    finally:
        for name, script in SCRIPTS.items():
            run(script, "stop", env={PORT_VARS[name]: str(ports[name])})
        for p in procs.values():
            p.terminate()
