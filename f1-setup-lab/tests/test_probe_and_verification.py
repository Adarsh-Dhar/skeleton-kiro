"""Task 6: setup-probe evidence and manual mapping confirmations. Nothing is ever verified automatically."""
from __future__ import annotations

import ast
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_game_udp import BASE, setup_packet

from db import create_db_engine, migrate
from f1lab.catalogs import ea_f1_25
from f1lab.catalogs.verification import confirm, factors_verified, get_probe_report, save_probe_report, status
from f1lab.integrations.game_udp.probe import SetupProbe
from f1lab.integrations.game_udp.recorder import RawRecorder

APP = Path(__file__).resolve().parents[1]
EXPECTED = {k: v["packet_field"] for k, v in ea_f1_25.GAME_FACTORS.items()}


@pytest.fixture
def engine(tmp_path):
    e = create_db_engine(tmp_path / "trialforge.db")
    migrate(e)
    return e


def test_catalog_code_keeps_every_factor_unverified():
    assert all(v["verified"] is False for v in ea_f1_25.GAME_FACTORS.values())
    # nothing in the backend writes the catalog's verified flag
    for p in (APP / "backend").rglob("*.py"):
        tree = ast.parse(p.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant) and t.slice.value == "verified":
                        pytest.fail(f"{p} assigns a 'verified' key")


def test_probe_detects_isolated_changes_and_cross_moves():
    p = SetupProbe(EXPECTED)
    p.observe({**BASE}, 0.0, 2025)
    p.observe({**BASE}, 0.1, 2025)  # no change
    p.observe({**BASE, "front_wing": 1}, 1.0, 2025)
    p.observe({**BASE, "front_wing": 50}, 2.0, 2025)
    p.observe({**BASE, "front_wing": 50, "rear_wing": 10, "brake_bias": 60}, 3.0, 2025)  # two fields together
    r = p.report()
    assert r["verified"] is False and all(f["verified"] is False for f in r["factors"].values())
    fw = r["factors"]["front_wing"]
    assert fw["status"] == "isolated_change_observed" and fw["low_seen"] == 1 and fw["high_seen"] == 50 and fw["isolated_changes"] == 2
    rw = r["factors"]["rear_wing"]
    assert rw["status"] == "moved_with_other_fields" and rw["co_moved_fields"] == ["brake_bias"]
    assert r["factors"]["rear_anti_roll_bar"]["status"] == "no_change_observed"
    assert r["other_fields_changed"] == ["brake_bias"] and r["game_variants"] == ["f1-25"]
    assert [c["changed"] for c in r["changes"]][0] == {"front_wing": [25.0, 1.0]}


def test_guided_probe_flags_a_wrong_mapping():
    p = SetupProbe(EXPECTED)
    p.observe(dict(BASE), 0, 2026)
    p.mark("rear_anti_roll_bar")
    p.observe({**BASE, "front_anti_roll_bar": 1}, 1, 2026)  # user moved rear ARB but the FRONT field changed
    p.observe({**BASE, "front_anti_roll_bar": 21}, 2, 2026)
    p.mark("front_wing")
    p.observe({**BASE, "front_anti_roll_bar": 21, "front_wing": 1}, 3, 2026)
    p.observe({**BASE, "front_anti_roll_bar": 21, "front_wing": 50}, 4, 2026)
    p.mark(None)
    r = p.report()
    assert r["factors"]["rear_anti_roll_bar"]["status"] == "different_field_changed"
    assert "catalog mapping may be wrong" in " ".join(r["factors"]["rear_anti_roll_bar"]["notes"])
    assert r["factors"]["front_wing"]["status"] == "matches_expected_field"
    assert r["game_variants"] == ["f1-25-2026"]
    with pytest.raises(ValueError):
        p.mark("brake_bias")


def test_confirmations_are_per_variant_and_need_evidence(engine):
    assert status(engine)["f1-25"]["front_wing"]["verified"] is False
    p = SetupProbe(EXPECTED)
    p.observe(dict(BASE), 0, 2025)
    p.observe({**BASE, "front_wing": 1}, 1, 2025)
    rid = save_probe_report(engine, p.report())
    assert get_probe_report(engine, rid)["factors"]["front_wing"]["low_seen"] == 1
    with pytest.raises(ValueError, match="must reference the probe report"):
        confirm(engine, "front_wing", "f1-25", verified=True, confirmed_by="me")
    with pytest.raises(ValueError, match="confirmed_by"):
        confirm(engine, "front_wing", "f1-25", verified=True, confirmed_by=" ", probe_report_id=rid)
    with pytest.raises(ValueError, match="recorded with"):
        confirm(engine, "front_wing", "f1-25-2026", verified=True, confirmed_by="me", probe_report_id=rid)
    s = confirm(engine, "front_wing", "f1-25", verified=True, confirmed_by="Adarsh", probe_report_id=rid)
    assert s["verified"] is True and "manual confirmation by Adarsh" in s["source"]
    st = status(engine)
    assert st["f1-25"]["front_wing"]["verified"] is True and st["f1-25-2026"]["front_wing"]["verified"] is False
    assert factors_verified(engine, ["front_wing"], "f1-25") and not factors_verified(engine, ["front_wing"])
    s = confirm(engine, "front_wing", "f1-25", verified=False, confirmed_by="Adarsh", probe_report_id=rid, note="screen showed 2")
    assert s["verified"] is False and "(rejected)" in s["source"]
    assert ea_f1_25.GAME_FACTORS["front_wing"]["verified"] is False  # the catalog code is never touched


def test_meta_and_live_experiment_report_unverified_by_default(client):
    m = client.get("/api/meta").json()
    for f in m["game"]["factors"].values():
        assert f["verified"] is False and set(f["verification"]) == {"f1-25", "f1-25-2026"}
        assert all("not confirmed" in s["source"] for s in f["verification"].values())
    v = client.get("/api/game/verification").json()
    assert v["status"]["f1-25"]["rear_wing"]["verified"] is False
    assert client.get("/api/game/probe-reports/nope").status_code == 404


def _send_setups(port, packets, delay=0.05):
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for pkt in packets:
        tx.sendto(pkt, ("127.0.0.1", port))
        time.sleep(delay)
    tx.close()


def _free_udp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_probe_cli_over_real_udp_stores_report_and_recording(tmp_path):
    db = tmp_path / "trialforge.db"
    port = _free_udp_port()
    out = tmp_path / "report.json"
    proc = subprocess.Popen([sys.executable, str(APP / "scripts" / "probe_setup.py"), "--port", str(port), "--seconds", "2.5", "--db", str(db), "--out", str(out)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(1.0)
    _send_setups(port, [setup_packet(), setup_packet(front_wing=1), setup_packet(front_wing=50), setup_packet(front_wing=50, rear_wing=3)])
    so, se = proc.communicate(timeout=30)
    assert proc.returncode == 0, se
    rep = json.loads(out.read_text())
    assert rep["verified"] is False and rep["factors"]["front_wing"]["status"] == "isolated_change_observed"
    assert rep["factors"]["front_wing"]["values_seen"] == [1.0, 25.0, 50.0]
    assert "NOT VERIFIED" in so
    e = create_db_engine(db)
    assert get_probe_report(e, rep["report_id"])["recording_id"] == rep["recording_id"]
    # the stored raw packets re-analyse to the same findings
    r2 = subprocess.run([sys.executable, str(APP / "scripts" / "probe_setup.py"), "--from-recording", rep["recording_id"], "--db", str(db), "--quiet",
                         "--out", str(tmp_path / "r2.json")], capture_output=True, text=True, timeout=30)
    assert r2.returncode == 0, r2.stderr
    rep2 = json.loads((tmp_path / "r2.json").read_text())
    assert rep2["factors"]["front_wing"]["values_seen"] == rep["factors"]["front_wing"]["values_seen"]


def test_verify_cli_status_and_confirm(tmp_path):
    db = tmp_path / "trialforge.db"
    e = create_db_engine(db)
    migrate(e)
    rec = RawRecorder(e, "probe-rec", source="probe")
    rec.close()
    p = SetupProbe(EXPECTED)
    p.observe(dict(BASE), 0, 2025)
    p.observe({**BASE, "rear_wing": 1}, 1, 2025)
    rid = save_probe_report(e, p.report(), "probe-rec")
    script = str(APP / "scripts" / "verify_setup_mapping.py")
    r = subprocess.run([sys.executable, script, "--db", str(db), "status"], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and r.stdout.count("not verified") == 8
    r = subprocess.run([sys.executable, script, "--db", str(db), "confirm", "--factor", "rear_wing", "--variant", "f1-25", "--report", rid, "--by", "tester"],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "VERIFIED" in r.stdout, r.stderr
    r = subprocess.run([sys.executable, script, "--db", str(db), "status"], capture_output=True, text=True, timeout=30)
    assert r.stdout.count("not verified") == 7
