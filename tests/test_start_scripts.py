"""Task 3: start scripts. Fast checks use the ``check`` command (no installs); a full start is opt-in (RUN_START_SMOKE=1)."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
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


@pytest.fixture(autouse=True)
def isolated_pid_dir(tmp_path, monkeypatch):
    """Record test runs in a temporary folder, so apps the developer has running on 8001/8002 neither break these tests nor
    get stopped by them."""
    monkeypatch.setenv("TRIALFORGE_PID_DIR", str(tmp_path / "pids"))


def run(script: Path, *args: str, env: dict | None = None, timeout: int = 60) -> subprocess.CompletedProcess:
    e = {**os.environ, **(env or {})}
    return subprocess.run(["bash", str(script), *args], capture_output=True, text=True, env=e, timeout=timeout, cwd=ROOT)


@pytest.mark.parametrize("name", ["f1", "wattwise"])
def test_scripts_are_valid_bash_and_executable(name):
    assert os.access(SCRIPTS[name], os.X_OK)
    assert subprocess.run(["bash", "-n", str(SCRIPTS[name])]).returncode == 0
    assert subprocess.run(["bash", "-n", str(ROOT / "scripts" / "lib" / "common.sh")]).returncode == 0


@pytest.mark.parametrize("name", ["f1", "wattwise"])
def test_check_reports_python_sdk_venv_port_and_root_db(name, tmp_path):
    r = run(SCRIPTS[name], "check", env={PORT_VARS[name]: str(free_port()), "TRIALFORGE_DB": str(tmp_path / "t.db"), "TRIALFORGE_DIST_DIR": str(tmp_path / "dist")})
    assert r.returncode == 0, r.stderr
    assert "python:" in r.stdout and "venv:" in r.stdout
    # both apps use the SDK wheel: the report names it and says it would be built from the empty dist dir
    assert "wheel:" in r.stdout and "missing (would be built)" in r.stdout and "trialforge_core-" in r.stdout
    assert "sdk:     not used" not in r.stdout
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


# ------------------------------------------------------------------------------------------- start-all and package.json
def test_package_json_scripts_point_at_existing_shell_scripts():
    import json

    pkg = json.loads((ROOT / "package.json").read_text())
    assert {"start", "stop", "status", "start-f1", "start-wattwise", "start-all"} <= set(pkg["scripts"])
    for name, cmd in pkg["scripts"].items():
        script = cmd.split()[1]
        assert cmd.startswith("bash scripts/") and (ROOT / script).exists(), (name, cmd)


def test_start_all_refuses_when_a_port_is_taken_and_starts_nothing():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    try:
        r = run(ROOT / "scripts" / "start-all.sh", "start", env={"F1_PORT": str(s.getsockname()[1]), "WATTWISE_PORT": str(free_port())})
    finally:
        s.close()
    assert r.returncode == 1 and "already in use" in r.stderr and "F1_PORT" in r.stderr
    assert "Starting" not in r.stdout


def test_start_all_check_stop_status_and_unknown_command():
    sa = ROOT / "scripts" / "start-all.sh"
    env = {"F1_PORT": str(free_port()), "WATTWISE_PORT": str(free_port())}
    r = run(sa, "check", env=env)
    assert r.returncode == 0 and r.stdout.count("python:") == 2
    assert "not running" in run(sa, "stop", env=env).stdout
    assert run(sa, "status", env=env).returncode == 1
    assert run(sa, "frobnicate").returncode == 1


# ------------------------------------------------------------------ wheel failure, health check and release script (task 9.4)

COMMON = ROOT / "scripts" / "lib" / "common.sh"
FAKE_SDK = "9.9.9"


def listening(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def pid_dead(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return True
    return False


class FakeHealth:
    """A fake backend: serves /api/health with the given JSON body (or a raw non-JSON answer when body is None)."""

    def __init__(self, body):
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                data = json.dumps(outer.body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.body = body
        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def health_check(port: int, timeout: float = 2, child: int | None = None) -> subprocess.CompletedProcess:
    """Run the real health_check from common.sh against the repository SDK version."""
    sdk = subprocess.run(["bash", "-c", f"source '{COMMON}'; sdk_version"], capture_output=True, text=True).stdout.strip()
    assert sdk
    cmd = f"source '{COMMON}'; health_check '{sys.executable}' {port} {child or os.getpid()} {timeout}"
    r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=30)
    r.sdk = sdk  # type: ignore[attr-defined]
    return r


def repo_sdk_version() -> str:
    return subprocess.run(["bash", "-c", f"source '{COMMON}'; sdk_version"], capture_output=True, text=True).stdout.strip()


def test_health_check_passes_when_trialforge_matches_the_repository_sdk():
    srv = FakeHealth({"status": "ok", "trialforge": repo_sdk_version()})
    try:
        r = health_check(srv.port)
    finally:
        srv.close()
    assert r.returncode == 0 and r.stdout.strip() == ""


def test_health_check_names_the_cause_for_each_failure():
    sdk = repo_sdk_version()
    srv = FakeHealth({"status": "ok", "trialforge": "0.0.1-other"})
    try:
        r = health_check(srv.port)
        assert r.returncode == 1 and "0.0.1-other" in r.stdout and sdk in r.stdout
        srv.body = {"status": "ok"}
        r = health_check(srv.port)
        assert r.returncode == 1 and "no 'trialforge' field" in r.stdout
    finally:
        srv.close()
    r = health_check(free_port(), timeout=2)  # nothing listens
    assert r.returncode == 1 and "no response" in r.stdout
    gone = subprocess.Popen(["true"])
    gone.wait()
    r = health_check(free_port(), timeout=2, child=gone.pid)
    assert r.returncode == 1 and "exited" in r.stdout


@pytest.fixture
def fake_tree(tmp_path):
    """A throw-away copy of the start script and common.sh with a stubbed venv, so run_app can be exercised without pip or network.

    The venv python passes pip/db.migrate calls and reports the SDK version; the app entry point is a fake backend whose
    /api/health body comes from FAKE_HEALTH (or it never listens when FAKE_HEALTH is 'none')."""
    root = tmp_path / "repo"
    (root / "scripts" / "lib").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "start-f1.sh", root / "scripts")
    shutil.copy(COMMON, root / "scripts" / "lib")
    sdk_dir = root / "skeleton" / "packages" / "trialforge-core"
    sdk_dir.mkdir(parents=True)
    (sdk_dir / "pyproject.toml").write_text(f'[project]\nname = "trialforge-core"\nversion = "{FAKE_SDK}"\n')
    (root / "pyproject.toml").write_text("[project]\nname = \"root\"\n")
    app = root / "f1-setup-lab"
    (app / ".venv" / "bin").mkdir(parents=True)
    (app / "pyproject.toml").write_text("[project]\nname = \"f1\"\n")
    py = app / ".venv" / "bin" / "python"
    py.write_text(
        "#!/bin/bash\n"
        'for a in "$@"; do case "$a" in pip|db.migrate) exit 0;; esac; done\n'
        f'case "$*" in *importlib.metadata*) echo {FAKE_SDK}; exit 0;; esac\n'
        f'exec "{sys.executable}" "$@"\n'
    )
    py.chmod(0o755)
    entry = app / ".venv" / "bin" / "f1-setup-lab"
    entry.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys, time\n"
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "open(os.environ['FAKE_PID_FILE'], 'w').write(str(os.getpid()))\n"
        "port = int(sys.argv[sys.argv.index('--port') + 1])\n"
        "mode = os.environ['FAKE_HEALTH']\n"
        "if mode == 'none':\n"
        "    time.sleep(120)\n"
        "body = json.dumps(json.loads(mode)).encode()\n"
        "class H(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)\n"
        "    def log_message(self, *a): pass\n"
        "HTTPServer(('127.0.0.1', port), H).serve_forever()\n"
    )
    entry.chmod(0o755)
    dist = tmp_path / "dist"
    dist.mkdir()
    return root, dist, tmp_path


def start_fake(fake_tree, health: str, *, wheel: bool = True, timeout: int = 3, **extra):
    root, dist, tmp = fake_tree
    if wheel:
        (dist / f"trialforge_core-{FAKE_SDK}-py3-none-any.whl").write_text("")
    port = free_port()
    env = {"F1_PORT": str(port), "TRIALFORGE_DB": str(tmp / "t.db"), "TRIALFORGE_DIST_DIR": str(dist), "HEALTH_TIMEOUT": str(timeout),
           "FAKE_HEALTH": health, "FAKE_PID_FILE": str(tmp / "child.pid"), "PYTHON_BIN": sys.executable, **extra}
    return port, env


@pytest.mark.parametrize(
    "health, cause",
    [
        (json.dumps({"status": "ok", "trialforge": "0.0.1-other"}), "0.0.1-other"),
        (json.dumps({"status": "ok"}), "no 'trialforge' field"),
        ("none", "no response"),
    ],
    ids=["differing", "missing-field", "no-response"],
)
def test_start_fails_naming_the_cause_and_stops_the_backend(fake_tree, health, cause):
    port, env = start_fake(fake_tree, health, timeout=2)
    r = run(fake_tree[0] / "scripts" / "start-f1.sh", "start", env=env)
    assert r.returncode == 1
    assert "health check failed" in r.stderr and cause in r.stderr
    child = int((fake_tree[2] / "child.pid").read_text())
    assert pid_dead(child) and not listening(port)
    assert not (fake_tree[2] / "pids" / "f1.pid").exists()


def test_start_keeps_running_when_health_matches(fake_tree):
    port, env = start_fake(fake_tree, json.dumps({"status": "ok", "trialforge": FAKE_SDK}), timeout=5)
    p = subprocess.Popen(["bash", str(fake_tree[0] / "scripts" / "start-f1.sh"), "start"], env={**os.environ, **env},
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.time() + 20
        while time.time() < deadline and not listening(port):
            time.sleep(0.2)
        assert listening(port)
        time.sleep(1.5)  # past the first health poll: a failed check would have stopped the backend
        assert p.poll() is None and listening(port)
    finally:
        p.terminate()
        try:
            p.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            p.kill()
            p.communicate()
    assert not listening(port)


def test_empty_wheel_directory_exits_1_and_never_starts_the_backend(fake_tree, tmp_path):
    root, dist, tmp = fake_tree
    port, env = start_fake(fake_tree, "none", wheel=False)
    shim = tmp / "python-no-venv"  # the wheel build needs a build venv; refuse to create one (no network, no build)
    shim.write_text(f'#!/bin/bash\nif [[ "$1" == "-m" && "$2" == "venv" ]]; then exit 1; fi\nexec "{sys.executable}" "$@"\n')
    shim.chmod(0o755)
    env["PYTHON_BIN"] = str(shim)
    assert list(dist.iterdir()) == []
    r = run(root / "scripts" / "start-f1.sh", "start", env=env)
    assert r.returncode == 1
    assert "could not create a build environment" in r.stderr
    assert "Starting" not in r.stdout
    assert not (tmp / "child.pid").exists() and not listening(port)
    assert not list(dist.glob("*.whl"))


def test_release_check_compares_the_sdk_for_both_apps():
    path = ROOT / "scripts" / "check-release.sh"
    assert subprocess.run(["bash", "-n", str(path)]).returncode == 0
    text = path.read_text()
    assert "F1 does not use the SDK" not in text and "not used by F1" not in text
    assert "both apps use the installed SDK $SDK_VERSION" in text
    assert "the SDK version, for both apps" in text
    assert 'body.get("trialforge") != sdk' in text and "F1 SETUP LAB and WATTWISE LAB need it" in text
