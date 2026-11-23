"""Subprocess tests for the restored CLI scripts (requirements 8.3-8.6).

The scripts are byte-identical to the release (8.1 forbids edits), so a case a script cannot meet is marked
``xfail(strict=False)`` with the gap named in its reason.
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
import time
from pathlib import Path

import pytest
from udp_packets import header, setup_packet, telemetry_packet

from db import create_db_engine, migrate
from f1lab.integrations.game_udp.recorder import RawRecorder, write_raw_file

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
APP = SCRIPTS.parent

# Every option string each script accepts (taken from its argparse definition).
ARGS = {
    "probe_setup.py": ["--host", "--port", "--seconds", "--guided", "--factors", "--from-recording", "--no-record", "--out", "--quiet", "--db"],
    "verify_setup_mapping.py": ["--db", "status", "reports", "show", "confirm", "--factor", "--variant", "--report", "--by", "--reject", "--note"],
    "inspect_udp_recording.py": ["recording_id", "--file", "--import", "--json", "--db"],
}
SCRIPT_NAMES = list(ARGS)


def run(name: str, *args: str, timeout: float = 60):
    return subprocess.run([sys.executable, str(SCRIPTS / name), *args], capture_output=True, text=True, timeout=timeout, cwd=APP, stdin=subprocess.DEVNULL)


def snapshot(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "trialforge.db"
    engine = create_db_engine(path)
    migrate(engine)
    engine.dispose()
    return path


@pytest.fixture
def recorded_db(tmp_path):
    """A database holding one recording built from udp_packets (setup, a setup change, telemetry)."""
    path = tmp_path / "rec" / "trialforge.db"
    path.parent.mkdir()
    engine = create_db_engine(path)
    migrate(engine)
    rec = RawRecorder(engine, "rec-1", source="live")
    for i, pkt in enumerate([setup_packet(), telemetry_packet(301), setup_packet(front_wing=30), telemetry_packet(318)]):
        rec.write(pkt, 100.0 + i)
    rec.close()
    engine.dispose()
    return path


# ---- 8.3 --help ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", SCRIPT_NAMES)
def test_help_lists_every_argument_quickly(name, tmp_path):
    before = snapshot(tmp_path)
    start = time.monotonic()
    r = run(name, "--help")
    elapsed = time.monotonic() - start
    assert r.returncode == 0, r.stderr
    for arg in ARGS[name]:
        assert arg in r.stdout, f"{name} --help does not list {arg}"
    assert elapsed < 5, f"{name} --help took {elapsed:.1f}s"
    assert snapshot(tmp_path) == before


# ---- 8.4 missing / unreadable input -------------------------------------------------------------------------

def test_inspect_missing_file(tmp_path):
    missing = tmp_path / "nope.f1raw"
    db = tmp_path / "t.db"
    r = run("inspect_udp_recording.py", "--file", str(missing), "--db", str(db))
    assert r.returncode == 1
    assert str(missing) in r.stderr
    assert "No such file" in r.stderr or "missing" in r.stderr.lower()
    assert not missing.exists()


def test_inspect_unreadable_file(tmp_path):
    f = tmp_path / "locked.f1raw"
    f.write_bytes(b"x")
    f.chmod(0)
    try:
        if f.stat().st_mode & 0o444 or open_ok(f):
            pytest.skip("cannot make a file unreadable (running as root?)")
        r = run("inspect_udp_recording.py", "--file", str(f), "--db", str(tmp_path / "t.db"))
        assert r.returncode == 1
        assert str(f) in r.stderr and ("Permission" in r.stderr or "unreadable" in r.stderr.lower())
    finally:
        f.chmod(0o644)
    assert f.read_bytes() == b"x"


def open_ok(p: Path) -> bool:
    try:
        p.read_bytes()
        return True
    except OSError:
        return False


@pytest.mark.xfail(strict=False, reason="gap: probe_setup.py --from-recording <missing id> exits via SystemExit(str) with 'has no packets'; "
                   "the message names the id but not 'missing'/'unreadable' as 8.4 requires")
def test_probe_missing_recording(db):
    before = db.read_bytes()
    r = run("probe_setup.py", "--from-recording", "no-such-rec", "--db", str(db))
    assert r.returncode == 1
    assert "no-such-rec" in r.stderr and ("missing" in r.stderr.lower() or "does not exist" in r.stderr.lower())
    assert db.read_bytes() == before


@pytest.mark.xfail(strict=False, reason="gap: verify_setup_mapping.py show <missing id> prints 'no report <id>' (exit 1) without a missing/unreadable reason")
def test_verify_missing_report(db):
    before = db.read_bytes()
    r = run("verify_setup_mapping.py", "--db", str(db), "show", "no-such-report")
    assert r.returncode == 1
    assert "no-such-report" in r.stderr and ("missing" in r.stderr.lower() or "does not exist" in r.stderr.lower())
    assert db.read_bytes() == before


def test_inspect_missing_recording_id(db):
    before = db.read_bytes()
    r = run("inspect_udp_recording.py", "no-such-rec", "--db", str(db))
    assert r.returncode == 1 and "no-such-rec" in r.stderr
    assert db.read_bytes() == before


# ---- 8.6 unknown / missing argument -------------------------------------------------------------------------

@pytest.mark.parametrize("name", SCRIPT_NAMES)
def test_unknown_argument_prints_usage(name):
    r = run(name, "--definitely-not-an-option")
    assert r.returncode != 0
    assert "usage:" in r.stderr.lower() and "error:" in r.stderr


def test_verify_missing_subcommand():
    r = run("verify_setup_mapping.py")
    assert r.returncode != 0 and "usage:" in r.stderr.lower()


def test_verify_confirm_missing_required_arguments():
    r = run("verify_setup_mapping.py", "confirm")
    assert r.returncode != 0
    assert "usage:" in r.stderr.lower() and "--factor" in r.stderr


# ---- 8.5 valid run with recorded fixture --------------------------------------------------------------------

def test_inspect_with_f1raw_fixture(tmp_path, db):
    raw = tmp_path / "cap.f1raw"
    write_raw_file(raw, [(100.0, setup_packet()), (101.0, telemetry_packet(301)), (102.0, setup_packet(front_wing=30)), (103.0, header(6) + b"")])
    r = run("inspect_udp_recording.py", "--file", str(raw), "--json", "--db", str(db))
    assert r.returncode == 0, r.stderr
    assert '"packets": 4' in r.stdout


def test_inspect_with_recorded_db(recorded_db):
    r = run("inspect_udp_recording.py", "rec-1", "--json", "--db", str(recorded_db))
    assert r.returncode == 0, r.stderr
    assert '"setup_changes": 1' in r.stdout


def test_probe_replays_recording(recorded_db, tmp_path):
    out = tmp_path / "report.json"
    r = run("probe_setup.py", "--from-recording", "rec-1", "--quiet", "--out", str(out), "--db", str(recorded_db))
    assert r.returncode == 0, r.stderr
    assert out.exists() and "probe report" in r.stdout


def test_verify_status_and_reports(recorded_db):
    for cmd in ("status", "reports"):
        r = run("verify_setup_mapping.py", "--db", str(recorded_db), cmd)
        assert r.returncode == 0, r.stderr
