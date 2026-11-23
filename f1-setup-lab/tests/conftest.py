"""Fixtures: one per-test database, a LabService factory and an API client on it. OpenF1 fakes live in openf1_fakes.py."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from f1lab.api.app import create_app
from f1lab.services.lab import LabService


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "trialforge.db")  # a per-test root database


@pytest.fixture
def lab_factory(db_path):
    """Zero-argument factory building a LabService on the per-test database (for ``create_app(db_path, lab_factory=...)``)."""
    return lambda: LabService(db_path)


@pytest.fixture
def client(db_path):
    with TestClient(create_app(db_path)) as c:
        yield c


def wait_done(client, exp_id, timeout=60):
    """Poll until no run is queued/running."""
    end = time.time() + timeout
    while time.time() < end:
        d = client.get(f"/api/experiments/{exp_id}").json()
        if d["counts"]["queued"] + d["counts"]["running"] == 0:
            return d
        time.sleep(0.05)
    raise AssertionError(f"experiment {exp_id} did not finish in {timeout}s")


FAST = {"simulator": {"latency_seconds": 0.0}}


@pytest.fixture
def make_experiment(client):
    counter = iter(range(1, 1000))

    def _make(**kw):
        body = {"name": "test experiment", "experiment_id": f"test-exp-{next(counter)}", **kw}
        body.setdefault("simulator", {"latency_seconds": 0.0})
        r = client.post("/api/experiments", json=body)
        assert r.status_code == 201, r.text
        return r.json()

    return _make


@pytest.fixture
def run_experiment(client, make_experiment):
    def _run(**kw):
        d = make_experiment(**kw)
        assert client.post(f"/api/experiments/{d['id']}/execute").status_code == 202
        return wait_done(client, d["id"])

    return _run
