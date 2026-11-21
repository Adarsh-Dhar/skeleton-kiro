"""Task 2: both apps share one database file but only see their own experiments."""
from __future__ import annotations

import time

import pytest
from f1lab.api.app import create_app as create_f1
from fastapi.testclient import TestClient
from sqlalchemy import text
from wattwise.api.app import create_app as create_ww

from db import create_db_engine, current_version, latest_version

BODY = {"name": "scoping test", "simulator": {"latency_seconds": 0.0}}


def _wait(client, exp_id, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        d = client.get(f"/api/experiments/{exp_id}").json()
        if d["counts"]["queued"] + d["counts"]["running"] == 0:
            return d
        time.sleep(0.05)
    raise AssertionError("did not finish")


@pytest.fixture
def both(tmp_path):
    path = str(tmp_path / "trialforge.db")
    with TestClient(create_f1(path)) as f1, TestClient(create_ww(path)) as ww:
        yield f1, ww, path


def test_each_app_lists_only_its_own_experiments(both):
    f1, ww, path = both
    assert f1.post("/api/experiments", json={**BODY, "experiment_id": "f1-a"}).status_code == 201
    assert ww.post("/api/experiments", json={**BODY, "experiment_id": "ww-a"}).status_code == 201
    assert [e["id"] for e in f1.get("/api/experiments").json()] == ["f1-a"]
    assert [e["id"] for e in ww.get("/api/experiments").json()] == ["ww-a"]
    with create_db_engine(path).connect() as c:
        rows = dict(c.execute(text("SELECT experiment_id, app FROM app_experiment_owner")).all())
    assert rows == {"f1-a": "f1-setup-lab", "ww-a": "wattwise-lab"}


def test_cross_app_fetch_is_not_found(both):
    f1, ww, _ = both
    f1.post("/api/experiments", json={**BODY, "experiment_id": "f1-x"})
    ww.post("/api/experiments", json={**BODY, "experiment_id": "ww-x"})
    assert ww.get("/api/experiments/f1-x").status_code == 404
    assert f1.get("/api/experiments/ww-x").status_code == 404
    assert ww.post("/api/experiments/f1-x/execute").status_code == 404
    assert f1.get("/api/experiments/f1-x").status_code == 200


def test_same_id_in_other_app_is_a_conflict_and_never_overwrites(both):
    f1, ww, _ = both
    f1.post("/api/experiments", json={**BODY, "name": "f1 original", "experiment_id": "shared-id"})
    r = ww.post("/api/experiments", json={**BODY, "experiment_id": "shared-id"})
    assert r.status_code == 409
    assert f1.get("/api/experiments/shared-id").json()["name"] == "f1 original"
    assert ww.get("/api/experiments").json() == []


def test_both_apps_run_experiments_on_one_file(both):
    f1, ww, path = both
    for client, eid in ((f1, "f1-run"), (ww, "ww-run")):
        assert client.post("/api/experiments", json={**BODY, "experiment_id": eid}).status_code == 201
        assert client.post(f"/api/experiments/{eid}/execute").status_code == 202
    d1, d2 = _wait(f1, "f1-run"), _wait(ww, "ww-run")
    assert d1["counts"]["completed"] > 0 and d2["counts"]["completed"] > 0


def test_restart_recovers_only_own_experiments(tmp_path):
    path = str(tmp_path / "trialforge.db")
    with TestClient(create_f1(path)) as f1:
        f1.post("/api/experiments", json={**BODY, "experiment_id": "f1-keep"})
    with TestClient(create_ww(path)) as ww:
        ww.post("/api/experiments", json={**BODY, "experiment_id": "ww-keep"})
    with TestClient(create_f1(path)) as f1, TestClient(create_ww(path)) as ww:
        assert [e["id"] for e in f1.get("/api/experiments").json()] == ["f1-keep"]
        assert [e["id"] for e in ww.get("/api/experiments").json()] == ["ww-keep"]


def test_app_startup_migrates_the_root_schema(tmp_path):
    path = str(tmp_path / "trialforge.db")
    with TestClient(create_ww(path)):
        pass
    assert current_version(create_db_engine(path)) == latest_version()


def test_default_database_is_the_root_database(monkeypatch, tmp_path):
    target = tmp_path / "from_env.db"
    monkeypatch.setenv("TRIALFORGE_DB", str(target))
    with TestClient(create_f1()):
        pass
    with TestClient(create_ww()):
        pass
    assert target.exists()
