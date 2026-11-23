"""Legacy stores -> root database. Fixtures are built the way the old WATTWISE LAB stored data (an SDK-only SQLite file and
tariffs.json). F1 SETUP LAB has no legacy store: it keeps only OpenF1 sessions, imported straight into the root database."""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

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
    from wattwise.api.app import create_app as ww_app

    ww = tmp_path / "wattwise-lab" / "data"
    ww.mkdir(parents=True)
    _make_legacy_sdk_db(ww / "wattwise.db", ww_app, ["ww-old-1", "ww-old-2"])
    custom = {"id": "my-own", "name": "My own tariff", "demand_charge_per_kw": 0.0, "periods": [{"label": "all", "start": 0, "end": 24, "price": 0.31}]}
    (ww / "tariffs.json").write_text(json.dumps([custom]))
    return tmp_path


def _run(legacy_root: Path, *extra: str) -> subprocess.CompletedProcess:
    args = [sys.executable, str(SCRIPT), "--target", str(legacy_root / "data" / "trialforge.db"),
            "--ww-db", str(legacy_root / "wattwise-lab/data/wattwise.db"), "--tariffs", str(legacy_root / "wattwise-lab/data/tariffs.json"), *extra]
    return subprocess.run(args, capture_output=True, text=True, timeout=300)


def test_dry_run_changes_nothing(legacy):
    before = {p: p.stat().st_mtime for p in legacy.rglob("*") if p.is_file()}
    r = _run(legacy, "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "DRY RUN" in r.stdout and "0 mismatch" in r.stdout
    assert not (legacy / "data" / "trialforge.db").exists()
    assert {p: p.stat().st_mtime for p in legacy.rglob("*") if p.is_file()} == before


def test_migrate_verify_rerun_and_the_app_sees_the_same_history(legacy):
    from wattwise.api.app import create_app as ww_app

    r = _run(legacy)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "verification passed" in r.stdout and "legacy stores kept" in r.stdout
    assert (legacy / "wattwise-lab/data/wattwise.db").exists()  # nothing deleted without --delete-legacy
    assert list((legacy / "data" / "backups").iterdir())
    target = str(legacy / "data" / "trialforge.db")
    with TestClient(ww_app(target)) as ww:
        assert sorted(e["id"] for e in ww.get("/api/experiments").json()) == ["ww-old-1", "ww-old-2"]
        assert ww.get("/api/experiments/ww-old-1").json()["counts"]["completed"] > 0
        assert ww.get("/api/experiments/ww-old-1/report").status_code == 200
        assert any(t["id"] == "my-own" for t in ww.get("/api/tariffs").json())
    counts = {t: sqlite3.connect(target).execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in ("experiments", "runs", "measurements", "analyses", "state_log")}
    r2 = _run(legacy)  # re-running is safe: nothing is duplicated
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert {t: sqlite3.connect(target).execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in counts} == counts


def test_failed_verification_deletes_nothing(legacy):
    assert _run(legacy).returncode == 0
    target = legacy / "data" / "trialforge.db"
    conn = sqlite3.connect(target)
    conn.execute("DELETE FROM runs WHERE run_id = (SELECT run_id FROM runs WHERE experiment_id='ww-old-2' LIMIT 1)")
    conn.commit()
    conn.close()
    r = _run(legacy, "--delete-legacy")
    assert r.returncode == 1 and "VERIFICATION FAILED" in r.stdout and "MISMATCH" in r.stdout
    assert (legacy / "wattwise-lab/data/wattwise.db").exists() and (legacy / "wattwise-lab/data/tariffs.json").exists()


def test_delete_legacy_after_success(legacy):
    r = _run(legacy, "--delete-legacy")
    assert r.returncode == 0, r.stdout + r.stderr
    for p in ("wattwise-lab/data/wattwise.db", "wattwise-lab/data/tariffs.json"):
        assert not (legacy / p).exists(), p
    shutil.rmtree(legacy / "data" / "backups")
    assert _run(legacy).stdout.count("no legacy stores found") == 1
