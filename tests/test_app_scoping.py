"""Both apps share one database file. Each app only sees its own experiments (F1 SETUP LAB also stores OpenF1 historical
sessions), and neither app touches the other's data."""
from __future__ import annotations

import time

import pytest
from f1lab.api.app import create_app as create_f1
from fastapi.testclient import TestClient
from sqlalchemy import text
from wattwise.api.app import create_app as create_ww

from db import create_db_engine, current_version, latest_version
from db.scoped import ScopedExperimentRepository

BODY = {"name": "scoping test", "simulator": {"latency_seconds": 0.0}}


def _wait(client, exp_id, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        d = client.get(f"/api/experiments/{exp_id}").json()
        if d["counts"]["queued"] + d["counts"]["running"] == 0:
            return d
        time.sleep(0.05)
    raise AssertionError("did not finish")


def _foreign_experiment(path, eid):
    """An experiment owned by F1 SETUP LAB in the same file, as one stored before F1 kept only historical data would be."""
    with TestClient(create_ww(path)) as ww:
        assert ww.post("/api/experiments", json={**BODY, "experiment_id": eid}).status_code == 201
    with create_db_engine(path).begin() as c:
        c.execute(text("UPDATE app_experiment_owner SET app = 'f1-setup-lab' WHERE experiment_id = :e"), {"e": eid})


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "trialforge.db")


def test_wattwise_lists_and_opens_only_its_own_experiments(path):
    _foreign_experiment(path, "other-x")
    with TestClient(create_ww(path)) as ww:
        assert ww.post("/api/experiments", json={**BODY, "experiment_id": "ww-a"}).status_code == 201
        assert [e["id"] for e in ww.get("/api/experiments").json()] == ["ww-a"]
        assert ww.get("/api/experiments/other-x").status_code == 404
        assert ww.post("/api/experiments/other-x/execute").status_code == 404
    with create_db_engine(path).connect() as c:
        owners = dict(c.execute(text("SELECT experiment_id, app FROM app_experiment_owner")).all())
    assert owners == {"other-x": "f1-setup-lab", "ww-a": "wattwise-lab"}


def test_an_id_owned_by_another_app_is_a_conflict_and_is_never_overwritten(path):
    _foreign_experiment(path, "shared-id")
    with TestClient(create_ww(path)) as ww:
        assert ww.post("/api/experiments", json={**BODY, "experiment_id": "shared-id"}).status_code == 409
    assert ScopedExperimentRepository("f1-setup-lab", path).get_spec("shared-id").name == "scoping test"


def test_both_apps_run_on_one_file_without_touching_each_other(path):
    with TestClient(create_ww(path)) as ww, TestClient(create_f1(path)) as f1:
        assert ww.post("/api/experiments", json={**BODY, "experiment_id": "ww-run"}).status_code == 201
        assert ww.post("/api/experiments/ww-run/execute").status_code == 202
        assert _wait(ww, "ww-run")["counts"]["completed"] > 0
        assert f1.get("/api/historical/sessions").json() == []  # F1 keeps historical sessions, not experiments
        assert f1.get("/api/experiments").json() == []  # F1 has its own experiments; WATTWISE's are not listed
        assert f1.get("/api/experiments/ww-run").status_code == 404
    with TestClient(create_f1(path)), TestClient(create_ww(path)) as ww:  # restart: nothing lost, nothing claimed by F1
        assert [e["id"] for e in ww.get("/api/experiments").json()] == ["ww-run"]
    with create_db_engine(path).connect() as c:
        assert c.execute(text("SELECT app, COUNT(*) FROM app_experiment_owner GROUP BY app")).all() == [("wattwise-lab", 1)]


def test_app_startup_migrates_the_root_schema(tmp_path):
    for create in (create_ww, create_f1):
        path = str(tmp_path / f"{create.__module__.split('.')[0]}.db")
        with TestClient(create(path)):
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
