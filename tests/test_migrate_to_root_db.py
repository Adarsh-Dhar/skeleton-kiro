"""Task 9: legacy stores -> root database. Fixtures are built the way the old apps stored data (SDK-only SQLite files,
Parquet sessions, evidence JSON/gz, .f1raw captures, tariffs.json)."""
from __future__ import annotations

import gzip
import json
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "migrate_to_root_db.py"
BODY = {"name": "legacy experiment", "simulator": {"latency_seconds": 0.0}}


def _wait(c, eid):
    for _ in range(600):
        d = c.get(f"/api/experiments/{eid}").json()
        if d["counts"]["queued"] + d["counts"]["running"] == 0:
            return d
        time.sleep(0.05)
    raise AssertionError("not finished")


def _make_legacy_sdk_db(path: Path, create_app, ids: list[str]) -> None:
    """Run real experiments, then strip everything except the SDK tables (= what the old app stored)."""
    with TestClient(create_app(str(path))) as c:
        for eid in ids:
            assert c.post("/api/experiments", json={**BODY, "experiment_id": eid}).status_code == 201
            c.post(f"/api/experiments/{eid}/execute")
            _wait(c, eid)
            c.post(f"/api/experiments/{eid}/analysis", json={})
    conn = sqlite3.connect(path)
    for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND (name LIKE 'app_%' OR name LIKE 'f1_%' OR name LIKE 'ww_%')").fetchall():
        conn.execute(f'DROP TABLE "{t}"')
    conn.commit()
    conn.close()


@pytest.fixture
def legacy(tmp_path):
    pytest.importorskip("pyarrow")
    from f1lab.api.app import create_app as f1_app
    from f1lab.integrations.game_udp.recorder import write_raw_file
    from wattwise.api.app import create_app as ww_app

    f1 = tmp_path / "f1-setup-lab" / "data"
    ww = tmp_path / "wattwise-lab" / "data"
    f1.mkdir(parents=True)
    ww.mkdir(parents=True)
    _make_legacy_sdk_db(f1 / "f1lab.db", f1_app, ["f1-old-1", "f1-old-2"])
    _make_legacy_sdk_db(ww / "wattwise.db", ww_app, ["ww-old-1"])
    s = f1 / "historical" / "sessions" / "fastf1-2024-italian-grand-prix-q"
    (s / "telemetry").mkdir(parents=True)
    (s / "position").mkdir()
    manifest = {"session_id": s.name, "provider": "fastf1", "year": 2024, "event": "Italian Grand Prix", "session_type": "Q",
                "drivers": [{"code": "VER", "number": "1"}], "channels": {"laps": True}}
    (s / "manifest.json").write_text(json.dumps(manifest))
    pd.DataFrame({"driver": ["VER", "VER"], "lap_number": pd.array([1, 2], dtype="Int64"), "lap_time_s": [80.1, 81.2],
                  "is_deleted": pd.array([False, True], dtype="boolean")}).to_parquet(s / "laps.parquet", index=False)
    pd.DataFrame({"lap_number": [1] * 50, "distance_m": [float(i) for i in range(50)], "speed_kph": [200.0] * 50}).to_parquet(s / "telemetry" / "ver.parquet", index=False)
    pd.DataFrame({"lap_number": [1, 1], "x_m": [0.0, 1.0], "y_m": [0.0, 1.0]}).to_parquet(s / "position" / "ver.parquet", index=False)
    (f1 / "historical" / "sessions" / "half-done").mkdir()  # an import that never completed: no manifest
    (f1 / "historical" / "fastf1-cache").mkdir()
    (f1 / "historical" / "fastf1-cache" / "x.ff1pkl").write_bytes(b"cached")
    ev = f1 / "evidence" / "f1-old-1"
    ev.mkdir(parents=True)
    (ev / "run-a.json").write_text(json.dumps({"accepted": True, "target": {"front_wing": 30}}))
    with gzip.open(ev / "run-a.telemetry.jsonl.gz", "wt") as fh:
        for i in range(7):
            fh.write(json.dumps({"session_time": float(i), "speed_kph": 100 + i}) + "\n")
    raw = f1 / "sessions" / "20250101T000000Z"
    raw.mkdir(parents=True)
    write_raw_file(raw / "raw.f1raw", [(1.0, b"a"), (2.0, b"bb"), (3.0, b"ccc")])
    custom = {"id": "my-own", "name": "My own tariff", "demand_charge_per_kw": 0.0, "periods": [{"label": "all", "start": 0, "end": 24, "price": 0.31}]}
    (ww / "tariffs.json").write_text(json.dumps([custom]))
    return tmp_path


def _run(legacy_root: Path, *extra: str) -> subprocess.CompletedProcess:
    args = [sys.executable, str(SCRIPT), "--target", str(legacy_root / "data" / "trialforge.db"),
            "--f1-db", str(legacy_root / "f1-setup-lab/data/f1lab.db"), "--ww-db", str(legacy_root / "wattwise-lab/data/wattwise.db"),
            "--f1-data", str(legacy_root / "f1-setup-lab/data"), "--tariffs", str(legacy_root / "wattwise-lab/data/tariffs.json"), *extra]
    return subprocess.run(args, capture_output=True, text=True, timeout=300)


def test_dry_run_changes_nothing(legacy):
    before = {p: p.stat().st_mtime for p in legacy.rglob("*") if p.is_file()}
    r = _run(legacy, "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "DRY RUN" in r.stdout and "0 mismatch" in r.stdout
    assert not (legacy / "data" / "trialforge.db").exists()
    assert {p: p.stat().st_mtime for p in legacy.rglob("*") if p.is_file()} == before


def test_migrate_verify_rerun_and_apps_see_the_same_history(legacy):
    from f1lab.api.app import create_app as f1_app
    from wattwise.api.app import create_app as ww_app

    r = _run(legacy)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "verification passed" in r.stdout and "legacy stores kept" in r.stdout
    assert (legacy / "f1-setup-lab/data/f1lab.db").exists()  # nothing deleted without --delete-legacy
    assert list((legacy / "data" / "backups").iterdir())
    target = str(legacy / "data" / "trialforge.db")
    with TestClient(f1_app(target)) as f1, TestClient(ww_app(target)) as ww:
        assert sorted(e["id"] for e in f1.get("/api/experiments").json()) == ["f1-old-1", "f1-old-2"]
        assert [e["id"] for e in ww.get("/api/experiments").json()] == ["ww-old-1"]
        d = f1.get("/api/experiments/f1-old-1").json()
        assert d["counts"]["completed"] > 0
        assert f1.get("/api/experiments/f1-old-1/report").status_code == 200
        assert ww.get("/api/experiments/f1-old-1").status_code == 404
        laps = f1.get("/api/historical/sessions/fastf1-2024-italian-grand-prix-q/laps").json()
        assert [x["is_deleted"] for x in laps] == [False, True]
        assert f1.get("/api/historical/sessions/fastf1-2024-italian-grand-prix-q/telemetry", params={"driver": "VER"}).json()["count"] == 50
        assert any(t["id"] == "my-own" for t in ww.get("/api/tariffs").json())
    from f1lab.integrations.game_udp.evidence import load_evidence, load_telemetry_series
    from f1lab.integrations.game_udp.recorder import read_raw

    from db import create_db_engine

    e = create_db_engine(target)
    assert load_evidence(e, "f1-old-1", "run-a")["accepted"] is True and len(load_telemetry_series(e, "f1-old-1", "run-a")) == 7
    assert [d for _, d in read_raw(e, "20250101T000000Z")] == [b"a", b"bb", b"ccc"]
    counts = {t: sqlite3.connect(target).execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in ("experiments", "runs", "measurements", "analyses", "state_log")}
    r2 = _run(legacy)  # re-running is safe: nothing is duplicated
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert {t: sqlite3.connect(target).execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in counts} == counts


def test_failed_verification_deletes_nothing(legacy):
    assert _run(legacy).returncode == 0
    target = legacy / "data" / "trialforge.db"
    conn = sqlite3.connect(target)
    conn.execute("DELETE FROM runs WHERE run_id = (SELECT run_id FROM runs WHERE experiment_id='f1-old-2' LIMIT 1)")
    conn.commit()
    conn.close()
    r = _run(legacy, "--delete-legacy")
    assert r.returncode == 1 and "VERIFICATION FAILED" in r.stdout and "MISMATCH" in r.stdout
    assert (legacy / "f1-setup-lab/data/f1lab.db").exists() and (legacy / "wattwise-lab/data/tariffs.json").exists()


def test_delete_legacy_after_success_and_cache_is_kept(legacy):
    r = _run(legacy, "--delete-legacy")
    assert r.returncode == 0, r.stdout + r.stderr
    for p in ("f1-setup-lab/data/f1lab.db", "wattwise-lab/data/wattwise.db", "wattwise-lab/data/tariffs.json", "f1-setup-lab/data/evidence",
              "f1-setup-lab/data/sessions", "f1-setup-lab/data/historical/sessions"):
        assert not (legacy / p).exists(), p
    assert (legacy / "data" / "fastf1-cache" / "x.ff1pkl").read_bytes() == b"cached"
    assert not list(legacy.rglob("*.parquet")) or all("backups" in str(p) for p in legacy.rglob("*.parquet"))
    shutil.rmtree(legacy / "data" / "backups")
    assert _run(legacy).stdout.count("no legacy stores found") == 1
