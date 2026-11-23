"""Verification harness scripts: historical session and UDP recording inspectors (restored; FastF1 smoke tests dropped)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_game_udp import history_packet, setup_packet, telemetry_packet
from openf1_fakes import fake_client

from db import create_db_engine, migrate
from f1lab.integrations.game_udp.recorder import RawRecorder
from f1lab.integrations.historical.openf1_importer import import_session_openf1
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
    client, _ = fake_client(tmp_path / "c")
    m = import_session_openf1(client, HistoricalRepository(e), {"year": 2025, "event": "Abu Dhabi", "session": "R"})
    r = _run("inspect_historical_session.py", "--db", str(path))
    assert r.returncode == 0 and m["session_id"] in r.stdout
    r = _run("inspect_historical_session.py", m["session_id"], "--json", "--db", str(path))
    assert r.returncode == 0, r.stderr
    info = json.loads(r.stdout)
    assert info["session_id"] == m["session_id"] and info["laps"]["total"] > 0
    assert set(info["per_driver"]) == {"HAM", "VER"}
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
