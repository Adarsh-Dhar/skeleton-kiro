"""Task 11 (F1-K5): real-data verification harness scripts.

The real FastF1 download runs only with RUN_NETWORK_TESTS=1 and is reported as SKIPPED (never passed) otherwise or
when the servers cannot be reached.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_game_udp import history_packet, setup_packet, telemetry_packet
from test_historical import _FakeClient

from db import create_db_engine, migrate
from f1lab.integrations.game_udp.recorder import RawRecorder
from f1lab.integrations.historical.importer import import_session
from f1lab.integrations.historical.repository import HistoricalRepository

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _run(name, *args, timeout=120):
    return subprocess.run([sys.executable, str(SCRIPTS / name), *args], capture_output=True, text=True, timeout=timeout)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "trialforge.db"
    e = create_db_engine(path)
    migrate(e)
    return path, e


def test_inspect_historical_session(db, tmp_path):
    path, e = db
    m = import_session(_FakeClient(), HistoricalRepository(e, tmp_path / "c"), {"year": 2024, "event": "Monza", "session": "Q", "drivers": ["VER", "LEC"]})
    r = _run("inspect_historical_session.py", "--db", str(path))
    assert r.returncode == 0 and m["session_id"] in r.stdout
    r = _run("inspect_historical_session.py", m["session_id"], "--json", "--db", str(path))
    assert r.returncode == 0, r.stderr
    info = json.loads(r.stdout)
    assert info["laps"]["total"] == 4 and info["laps"]["deleted"] == 1 and info["per_driver"]["VER"]["telemetry_samples"] == 8
    assert "brake_pct" not in info["per_driver"]["VER"]["missing_channels"]  # boolean brake stored as brake_applied
    assert info["weather"]["samples"] == 1 and info["corners"] == 0
    assert _run("inspect_historical_session.py", "nope", "--db", str(path)).returncode == 1


def test_inspect_udp_recording(db, tmp_path):
    path, e = db
    rec = RawRecorder(e, "rec-1", source="live")
    for i, pkt in enumerate([setup_packet(), history_packet(1), telemetry_packet(301), setup_packet(front_wing=30), b"junk", telemetry_packet(318)]):
        rec.write(pkt, 100.0 + i)
    rec.close()
    r = _run("inspect_udp_recording.py", "--db", str(path))
    assert "rec-1" in r.stdout and "packets=6" in r.stdout
    r = _run("inspect_udp_recording.py", "rec-1", "--json", "--db", str(path))
    assert r.returncode == 0, r.stderr
    s = json.loads(r.stdout)
    assert s["packets"] == 6 and s["malformed"] == 1 and s["setup_changes"] == 1
    assert s["setups"][1]["changed"] == {"front_wing": [25.0, 30.0]} or s["setups"][1]["changed"]["front_wing"][1] == 30
    assert s["telemetry"]["samples"] == 2 and s["telemetry"]["top_speed_kph"] == pytest.approx(318, abs=0.5)


def test_smoke_script_reports_skipped_without_network(monkeypatch, capsys):
    sys.path.insert(0, str(SCRIPTS))
    import smoke_fastf1_import as smoke

    monkeypatch.setattr(smoke, "network_available", lambda timeout=5.0: (False, "test: offline"))
    rc = smoke.main(["--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == smoke.SKIPPED == 3 and out["result"] == "SKIPPED" and "NOT a pass" in out["reason"]


@pytest.mark.skipif(not os.environ.get("RUN_NETWORK_TESTS"), reason="real FastF1 download is opt-in (RUN_NETWORK_TESTS=1); reported as SKIPPED, not passed")
def test_real_fastf1_import_smoke(tmp_path):
    r = _run("smoke_fastf1_import.py", "--year", "2024", "--event", "Monza", "--session", "Q", "--drivers", "VER", "LEC",
             "--db", str(tmp_path / "trialforge.db"), "--json", timeout=1800)
    out = json.loads(r.stdout[r.stdout.index("{"):])
    if r.returncode == 3:
        pytest.skip(f"SKIPPED: {out['reason']}")
    assert r.returncode == 0, out
    assert out["result"] == "PASSED" and all(out["checks"].values())
